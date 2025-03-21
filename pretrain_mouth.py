#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

import os
import random
import torch
from torch_ema import ExponentialMovingAverage
from random import randint
from utils.loss_utils import l1_loss, l2_loss, patchify, ssim
from gaussian_renderer import render, render_motion, render_motion_mouth_con
import sys, copy
from scene_pretrain import Scene, GaussianModel, MouthMotionNetwork, MotionNetwork
from utils.general_utils import safe_state
import lpips
import uuid
from tqdm import tqdm
from utils.image_utils import psnr
from argparse import ArgumentParser, Namespace
from arguments import ModelParams, PipelineParams, OptimizationParams
try:
    from tensorboardX import SummaryWriter
    TENSORBOARD_FOUND = True
except ImportError:
    TENSORBOARD_FOUND = False

def training(dataset, opt, pipe, testing_iterations, saving_iterations, checkpoint_iterations, checkpoint, debug_from):
    data_list = [
        "macron", "shaheen", "may", "jaein", "obama1" 
    ]

    testing_iterations = [i * len(data_list) for i in range(0, opt.iterations + 1, 2000)]
    checkpoint_iterations =  saving_iterations = [i * len(data_list) for i in range(0, opt.iterations + 1, 5000)] + [opt.iterations * len(data_list)]

    # vars
    warm_step = 3000 * len(data_list)
    opt.densify_until_iter = (opt.iterations - 1000) * len(data_list)
    lpips_start_iter = 999999999999
    motion_stop_iter = opt.iterations * len(data_list)
    mouth_select_iter = (opt.iterations - 10000) * len(data_list)
    p_motion_start_iter = 0
    mouth_step = 1 / max(mouth_select_iter, 1)
    select_interval = 7

    opt.iterations *= len(data_list)

    first_iter = 0
    tb_writer = prepare_output_and_logger(dataset)
    
    scene_list = []
    for data_name in data_list:  
        gaussians = GaussianModel(dataset)
        _dataset = copy.deepcopy(dataset)
        _dataset.source_path = os.path.join(dataset.source_path, data_name)
        _dataset.model_path = os.path.join(dataset.model_path, data_name)
        
        os.makedirs(_dataset.model_path, exist_ok = True)
        with open(os.path.join(_dataset.model_path, "cfg_args"), 'w') as cfg_log_f:
            cfg_log_f.write(str(Namespace(**vars(_dataset))))
        
        scene = Scene(_dataset, gaussians)
        scene_list.append(scene)
        gaussians.training_setup(opt)
        
        with torch.no_grad():
            gaussians._xyz /= 2
            gaussians._xyz[:,1] -= 0.05
        
        _dataset_face = copy.deepcopy(dataset)
        _dataset_face.type = "face"
        gaussians_face = GaussianModel(_dataset_face)
        (model_params, _, _, _) = torch.load(os.path.join(_dataset.model_path, "chkpnt_face_latest.pth"))
        gaussians_face.restore(model_params, None)
        scene.gaussians_2 = gaussians_face

    motion_net = MouthMotionNetwork(args=dataset).cuda()
    motion_optimizer = torch.optim.AdamW(motion_net.get_params(5e-3, 5e-4), betas=(0.9, 0.99), eps=1e-8)
    scheduler = torch.optim.lr_scheduler.LambdaLR(motion_optimizer, lambda iter: (0.5 ** (iter / mouth_select_iter)) if iter < mouth_select_iter else 0.1 ** (iter / opt.iterations))
    ema_motion_net = ExponentialMovingAverage(motion_net.parameters(), decay=0.995)
    
    with torch.no_grad():
        motion_net_face = MotionNetwork(args=dataset).cuda()
        (motion_params, _, _) = torch.load(os.path.join(dataset.model_path, "chkpnt_ema_face_latest.pth"))
        # gaussians.restore(model_params, opt)
        motion_net_face.load_state_dict(motion_params)

    lpips_criterion = lpips.LPIPS(net='alex').eval().cuda()

    bg_color = [0, 1, 0] # if dataset.white_background else [0, 0, 0]
    background = torch.tensor(bg_color, dtype=torch.float32, device="cuda")


    iter_start = torch.cuda.Event(enable_timing = True)
    iter_end = torch.cuda.Event(enable_timing = True)

    viewpoint_stack = None
    ema_loss_for_log = 0.0
    progress_bar = tqdm(range(first_iter, opt.iterations), ascii=True, dynamic_ncols=True, desc="Training progress")
    first_iter += 1
    for iteration in range(first_iter, opt.iterations + 1):        

        iter_start.record()
        
        cur_scene_idx = randint(0, len(scene_list)-1)
        scene = scene_list[cur_scene_idx]
        gaussians = scene.gaussians
        
        gaussians.update_learning_rate(iteration)

        # Every 1000 its we increase the levels of SH up to a maximum degree
        if iteration % 1000 == 0:
            gaussians.oneupSHdegree()

        # Pick a random Camera
        # if not viewpoint_stack:
        viewpoint_stack = scene.getTrainCameras().copy()
        viewpoint_cam = viewpoint_stack.pop(randint(0, len(viewpoint_stack)-1))

        # find a big mouth

        au_global_lb = viewpoint_cam.talking_dict['au25'][1]
        au_global_ub = viewpoint_cam.talking_dict['au25'][4]
        au_window = (au_global_ub - au_global_lb) * 0.2

        au_ub = au_global_ub
        au_lb = au_ub - mouth_step * iteration * (au_global_ub - au_global_lb)

        if iteration < warm_step:
            while viewpoint_cam.talking_dict['au25'][0] < au_global_ub:
                if not viewpoint_stack:
                    viewpoint_stack = scene.getTrainCameras().copy()
                viewpoint_cam = viewpoint_stack.pop(randint(0, len(viewpoint_stack)-1))

        if warm_step < iteration < mouth_select_iter:
            if iteration % select_interval == 0:
                while viewpoint_cam.talking_dict['au25'][0] < au_lb or viewpoint_cam.talking_dict['au25'][0] > au_ub:
                    if not viewpoint_stack:
                        viewpoint_stack = scene.getTrainCameras().copy()
                    viewpoint_cam = viewpoint_stack.pop(randint(0, len(viewpoint_stack)-1))

            while torch.as_tensor(viewpoint_cam.talking_dict["mouth_mask"]).cuda().sum() < 20:
                if not viewpoint_stack:
                    viewpoint_stack = scene.getTrainCameras().copy()
                viewpoint_cam = viewpoint_stack.pop(randint(0, len(viewpoint_stack)-1))


        # Render
        if (iteration - 1) == debug_from:
            pipe.debug = True

        # if iteration > bg_iter:
        #     # turn to black
        #     bg_color = [0, 0, 0] # if dataset.white_background else [0, 0, 0]
        #     background = torch.tensor(bg_color, dtype=torch.float32, device="cuda")

        face_mask = torch.as_tensor(viewpoint_cam.talking_dict["face_mask"]).cuda()
        hair_mask = torch.as_tensor(viewpoint_cam.talking_dict["hair_mask"]).cuda()
        mouth_mask = torch.as_tensor(viewpoint_cam.talking_dict["mouth_mask"]).cuda()
        head_mask =  face_mask + hair_mask
        
        [xmin, xmax, ymin, ymax] = viewpoint_cam.talking_dict['lips_rect']
        lips_mask = torch.zeros_like(mouth_mask)
        lips_mask[xmin:xmax, ymin:ymax] = True

        if iteration < warm_step:
            render_pkg = render(viewpoint_cam, gaussians, pipe, background)
        elif iteration < p_motion_start_iter:
            render_pkg = render_motion_mouth_con(viewpoint_cam, gaussians, motion_net, scene.gaussians_2, motion_net_face, pipe, background)
        else:
            render_pkg = render_motion_mouth_con(viewpoint_cam, gaussians, motion_net, scene.gaussians_2, motion_net_face, pipe, background, personalized=True)
            
        image_green, alpha, viewspace_point_tensor, visibility_filter, radii = render_pkg["render"], render_pkg["alpha"], render_pkg["viewspace_points"], render_pkg["visibility_filter"], render_pkg["radii"]
        
        gt_image  = viewpoint_cam.original_image.cuda() / 255.0
        gt_image_green = gt_image * mouth_mask + background[:, None, None] * ~mouth_mask

        if iteration > motion_stop_iter:
            for param in motion_net.parameters():
                param.requires_grad = False
        # if iteration > bg_iter:
        #     gaussians._xyz.requires_grad = False
        #     gaussians._opacity.requires_grad = False
        #     # gaussians._features_dc.requires_grad = False
        #     # gaussians._features_rest.requires_grad = False
        #     gaussians._scaling.requires_grad = False
        #     gaussians._rotation.requires_grad = False
        
        # Loss            
        image_green[:, (lips_mask ^ mouth_mask)] = background[:, None]

        Ll1 = l1_loss(image_green, gt_image_green)
        loss = Ll1 + opt.lambda_dssim * (1.0 - ssim(image_green, gt_image_green))

        if iteration > warm_step:
            loss += 1e-5 * (render_pkg['motion']['d_xyz'].abs()).mean()
            loss += 1e-5 * (render_pkg['motion']['d_rot'].abs()).mean()
            loss += 1e-3 * (((1-alpha) * lips_mask).mean() + (alpha * ~lips_mask).mean())

            if iteration > p_motion_start_iter:
                loss += 1e-5 * (render_pkg['p_motion']['d_xyz'].abs()).mean()
                loss += 1e-5 * (render_pkg['p_motion']['d_rot'].abs()).mean()
                # loss += 1e-5 * (render_pkg['p_motion']['p_xyz'].abs()).mean()
                # loss += 1e-5 * (render_pkg['p_motion']['p_scale'].abs()).mean()
        
                # Contrast
                audio_feat = viewpoint_cam.talking_dict["auds"].cuda()
                p_motion_preds = gaussians.neural_motion_grid(gaussians.get_xyz, audio_feat)
                with torch.no_grad():
                    tmp_scene_idx = randint(0, len(scene_list)-1)
                    while tmp_scene_idx == cur_scene_idx: tmp_scene_idx = randint(0, len(scene_list)-1)
                    tmp_scene = scene_list[tmp_scene_idx]
                    tmp_gaussians = tmp_scene.gaussians
                    tmp_p_motion_preds = tmp_gaussians.neural_motion_grid(gaussians.get_xyz, audio_feat)
                contrast_loss = (tmp_p_motion_preds['d_xyz'] * p_motion_preds['d_xyz']).sum(-1)
                contrast_loss[contrast_loss < 0] = 0
                loss += contrast_loss.mean()
                
        image_t = image_green.clone()
        gt_image_t = gt_image_green.clone()

        if iteration > lpips_start_iter:
            patch_size = random.randint(16, 21) * 2
            loss += 0.5 * lpips_criterion(patchify(image_t[None, ...] * 2 - 1, patch_size), patchify(gt_image_t[None, ...] * 2 - 1, patch_size)).mean()



        loss.backward()

        iter_end.record()

        with torch.no_grad():
            # Progress bar
            ema_loss_for_log = 0.4 * loss.item() + 0.6 * ema_loss_for_log
            if iteration % 10 == 0:
                progress_bar.set_postfix({"Loss": f"{ema_loss_for_log:.{5}f}", "AU25": f"{au_lb:.{1}f}-{au_ub:.{1}f}"})
                progress_bar.update(10)
            if iteration == opt.iterations:
                progress_bar.close()

            # if (iteration in saving_iterations):
            #     print("\n[ITER {}] Saving Gaussians".format(iteration))
            #     scene.save(str(iteration)+'_mouth')

            # Log and save
            training_report(tb_writer, iteration, Ll1, loss, l1_loss, iter_start.elapsed_time(iter_end), testing_iterations, scene, motion_net, motion_net_face, render if iteration < warm_step else render_motion_mouth_con, (pipe, background))
            if (iteration in checkpoint_iterations):
                print("\n[ITER {}] Saving Checkpoint".format(iteration))
                ckpt = (motion_net.state_dict(), motion_optimizer.state_dict(), iteration)
                torch.save(ckpt, dataset.model_path + "/chkpnt_mouth_latest" + ".pth")
                with ema_motion_net.average_parameters():
                    ckpt_ema = (motion_net.state_dict(), motion_optimizer.state_dict(), iteration)
                    torch.save(ckpt, dataset.model_path + "/chkpnt_ema_mouth_latest" + ".pth")
                for _scene in scene_list:
                    _gaussians = _scene.gaussians
                    ckpt = (_gaussians.capture(), motion_net.state_dict(), motion_optimizer.state_dict(), iteration)
                    torch.save(ckpt, _scene.model_path + "/chkpnt_mouth_" + str(iteration) + ".pth")
                    torch.save(ckpt, _scene.model_path + "/chkpnt_mouth_latest" + ".pth")


            # Densification
            if iteration < opt.densify_until_iter:
                # Keep track of max radii in image-space for pruning
                gaussians.max_radii2D[visibility_filter] = torch.max(gaussians.max_radii2D[visibility_filter], radii[visibility_filter])
                gaussians.add_densification_stats(viewspace_point_tensor, visibility_filter)

                if iteration > opt.densify_from_iter and iteration % opt.densification_interval == 0:
                    size_threshold = 20 if iteration > opt.opacity_reset_interval else None
                    gaussians.densify_and_prune(opt.densify_grad_threshold, 0.05 + 0.25 * iteration / opt.densify_until_iter, scene.cameras_extent, size_threshold)

                    shs_view = gaussians.get_features.transpose(1, 2).view(-1, 3, (gaussians.max_sh_degree+1)**2)
                    dir_pp = (gaussians.get_xyz - viewpoint_cam.camera_center.repeat(gaussians.get_features.shape[0], 1))
                    dir_pp_normalized = dir_pp/dir_pp.norm(dim=1, keepdim=True)
                    from utils.sh_utils import eval_sh
                    sh2rgb = eval_sh(gaussians.active_sh_degree, shs_view, dir_pp_normalized)
                    colors_precomp = torch.clamp_min(sh2rgb + 0.5, 0.0)

                    bg_color_mask = (colors_precomp[..., 0] < 20/255) * (colors_precomp[..., 1] > 235/255) * (colors_precomp[..., 2] < 20/255)
                    gaussians.xyz_gradient_accum[bg_color_mask] /= 2
                    gaussians._opacity[bg_color_mask] = gaussians.inverse_opacity_activation(torch.ones_like(gaussians._opacity[bg_color_mask]) * 0.1)
                    gaussians._scaling[bg_color_mask] /= 10

                # if iteration % opt.opacity_reset_interval == 0 or (dataset.white_background and iteration == opt.densify_from_iter):
                #     gaussians.reset_opacity()

            # Optimizer step
            if iteration < opt.iterations:
                motion_optimizer.step()
                gaussians.optimizer.step()

                motion_optimizer.zero_grad()
                gaussians.optimizer.zero_grad(set_to_none = True)

                scheduler.step()
                ema_motion_net.update()


def prepare_output_and_logger(args):    
    if not args.model_path:
        if os.getenv('OAR_JOB_ID'):
            unique_str=os.getenv('OAR_JOB_ID')
        else:
            unique_str = str(uuid.uuid4())
        args.model_path = os.path.join("./output/", unique_str[0:10])
        
    # Set up output folder
    print("Output folder: {}".format(args.model_path))
    os.makedirs(args.model_path, exist_ok = True)
    with open(os.path.join(args.model_path, "cfg_args"), 'w') as cfg_log_f:
        cfg_log_f.write(str(Namespace(**vars(args))))

    # Create Tensorboard writer
    tb_writer = None
    if TENSORBOARD_FOUND:
        tb_writer = SummaryWriter(args.model_path)
    else:
        print("Tensorboard not available: not logging progress")
    return tb_writer

def training_report(tb_writer, iteration, Ll1, loss, l1_loss, elapsed, testing_iterations, scene : Scene, motion_net, motion_net_face, renderFunc, renderArgs):
    # Report test and samples of training set
    if iteration in testing_iterations:
        torch.cuda.empty_cache()
        validation_configs = ({'name': 'test', 'cameras' : [scene.getTestCameras()[idx % len(scene.getTestCameras())] for idx in range(5, 100, 10)]}, 
                              {'name': 'train', 'cameras' : [scene.getTrainCameras()[idx % len(scene.getTrainCameras())] for idx in range(5, 30, 5)]})

        for config in validation_configs:
            if config['cameras'] and len(config['cameras']) > 0:
                l1_test = 0.0
                psnr_test = 0.0
                for idx, viewpoint in enumerate(config['cameras']):
                    render_pkg_p = None
                    if renderFunc is render:
                        render_pkg = renderFunc(viewpoint, scene.gaussians, *renderArgs)
                    else:
                        render_pkg = renderFunc(viewpoint, scene.gaussians, motion_net, scene.gaussians_2, motion_net_face, *renderArgs)
                        render_pkg_p = renderFunc(viewpoint, scene.gaussians, motion_net, scene.gaussians_2, motion_net_face, personalized=True, *renderArgs)

                    image = torch.clamp(render_pkg["render"], 0.0, 1.0)
                    alpha = render_pkg["alpha"]
                    # image = image - renderArgs[1][:, None, None] * (1.0 - alpha) + viewpoint.background.cuda() / 255.0 * (1.0 - alpha)
                    image = image
                    # gt_image = torch.clamp(viewpoint.original_image.to("cuda") / 255.0, 0.0, 1.0)
                    gt_image = torch.clamp(viewpoint.original_image.to("cuda") / 255.0, 0.0, 1.0) * alpha + renderArgs[1][:, None, None] * (1.0 - alpha)
                    if tb_writer and (idx < 5):
                        tb_writer.add_images(config['name'] + "_view_{}_mouth/render".format(viewpoint.image_name), image[None], global_step=iteration)
                        if render_pkg_p is not None:
                            tb_writer.add_images(config['name'] + "_view_{}_mouth/render_p".format(viewpoint.image_name), render_pkg_p["render"][None], global_step=iteration)
                        tb_writer.add_images(config['name'] + "_view_{}_mouth/ground_truth".format(viewpoint.image_name), gt_image[None], global_step=iteration)
                        tb_writer.add_images(config['name'] + "_view_{}_mouth/depth".format(viewpoint.image_name), (render_pkg["depth"] / render_pkg["depth"].max())[None], global_step=iteration)


                    l1_test += l1_loss(image, gt_image).mean().double()
                    psnr_test += psnr(image, gt_image).mean().double()
                psnr_test /= len(config['cameras'])
                l1_test /= len(config['cameras'])          
                print("\n[ITER {}] Evaluating {}: L1 {} PSNR {}".format(iteration, config['name'], l1_test, psnr_test))
                if tb_writer:
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - l1_loss', l1_test, iteration)
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - psnr', psnr_test, iteration)

        torch.cuda.empty_cache()

if __name__ == "__main__":
    # Set up command line argument parser
    parser = ArgumentParser(description="Training script parameters")
    lp = ModelParams(parser)
    op = OptimizationParams(parser)
    pp = PipelineParams(parser)
    parser.add_argument('--ip', type=str, default="127.0.0.1")
    parser.add_argument('--port', type=int, default=6009)
    parser.add_argument('--debug_from', type=int, default=-1)
    parser.add_argument('--detect_anomaly', action='store_true', default=False)
    parser.add_argument("--test_iterations", nargs="+", type=int, default=[])
    parser.add_argument("--save_iterations", nargs="+", type=int, default=[])
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--checkpoint_iterations", nargs="+", type=int, default=[])
    parser.add_argument("--start_checkpoint", type=str, default = None)
    args = parser.parse_args(sys.argv[1:])
    args.save_iterations.append(args.iterations)
    
    print("Optimizing " + args.model_path)

    # Initialize system state (RNG)
    safe_state(args.quiet)

    # Start GUI server, configure and run training
    torch.autograd.set_detect_anomaly(args.detect_anomaly)
    training(lp.extract(args), op.extract(args), pp.extract(args), args.test_iterations, args.save_iterations, args.checkpoint_iterations, args.start_checkpoint, args.debug_from)

    # All done
    print("\nTraining complete.")
