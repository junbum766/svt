import argparse
import json
import os
import torch
import torch.backends.cudnn as cudnn
from pathlib import Path
from torch import nn
from tqdm import tqdm

from datasets import UCF101, HMDB51, Kinetics
from models import get_vit_base_patch16_224, get_aux_token_vit, SwinTransformer3D
from utils import utils
from utils.meters import TestMeter
from utils.parser import load_config


def eval_linear(args):
    utils.init_distributed_mode(args)
    print("git:\n  {}\n".format(utils.get_sha()))
    print("\n".join("%s: %s" % (k, str(v)) for k, v in sorted(dict(vars(args)).items())))
    cudnn.benchmark = True
    os.makedirs(args.output_dir, exist_ok=True)
    json.dump(vars(args), open(f"{args.output_dir}/config.json", "w"), indent=4)

    # ============ preparing data ... ============
    config = load_config(args)
    # config.DATA.PATH_TO_DATA_DIR = f"{os.path.expanduser('~')}/repo/mmaction2/data/{args.dataset}/splits"
    # config.DATA.PATH_PREFIX = f"{os.path.expanduser('~')}/repo/mmaction2/data/{args.dataset}/videos"
    config.TEST.NUM_SPATIAL_CROPS = 1
    if args.dataset == "ucf101":
        dataset_train = UCF101(cfg=config, mode="train", num_retries=10)
        dataset_val = UCF101(cfg=config, mode="val", num_retries=10)
        config.TEST.NUM_SPATIAL_CROPS = 3
        multi_crop_val = UCF101(cfg=config, mode="val", num_retries=10)
    elif args.dataset == "hmdb51":
        dataset_train = HMDB51(cfg=config, mode="train", num_retries=10)
        dataset_val = HMDB51(cfg=config, mode="val", num_retries=10)
        config.TEST.NUM_SPATIAL_CROPS = 3
        multi_crop_val = HMDB51(cfg=config, mode="val", num_retries=10)
    elif args.dataset == "kinetics400":
        dataset_train = Kinetics(cfg=config, mode="train", num_retries=10)
        dataset_val = Kinetics(cfg=config, mode="val", num_retries=10)
        config.TEST.NUM_SPATIAL_CROPS = 3
        multi_crop_val = Kinetics(cfg=config, mode="val", num_retries=10)
    else:
        raise NotImplementedError(f"invalid dataset: {args.dataset}")

    sampler = torch.utils.data.distributed.DistributedSampler(dataset_train)
    train_loader = torch.utils.data.DataLoader(
        dataset_train,
        sampler=sampler,
        batch_size=args.batch_size_per_gpu,
        num_workers=args.num_workers,
        pin_memory=True,
    )
    val_loader = torch.utils.data.DataLoader(
        dataset_val,
        batch_size=args.batch_size_per_gpu,
        num_workers=args.num_workers,
        pin_memory=True,
    )

    multi_crop_val_loader = torch.utils.data.DataLoader(
        multi_crop_val,
        batch_size=args.batch_size_per_gpu,
        num_workers=args.num_workers,
        pin_memory=True,
    )

    print(f"Data loaded with {len(dataset_train)} train and {len(dataset_val)} val imgs.")

    # ============ building network ... ============
    if config.DATA.USE_FLOW or config.MODEL.TWO_TOKEN:
        model = get_aux_token_vit(cfg=config, no_head=True)
        model_embed_dim = 2 * model.embed_dim
    else:
        if args.arch == "vit_base":
            # model = get_vit_base_patch16_224(cfg=config, no_head=True)
            # model_embed_dim = model.embed_dim
            
            # model_embed_dim = 768 ### vit
            model = get_vit_base_patch16_224(cfg=config, no_head=False) ### plus linear head on vit
            model_embed_dim = model.embed_dim
            ### model = model + linear head
            
        elif args.arch == "swin":
            model = SwinTransformer3D(depths=[2, 2, 18, 2], embed_dim=128, num_heads=[4, 8, 16, 32])
            model_embed_dim = 1024
        else:
            raise Exception(f"invalid model: {args.arch}")

    ckpt = torch.load(args.pretrained_weights)
    #  select_ckpt = 'motion_teacher' if args.use_flow else "teacher"
    if "teacher" in ckpt:
        ckpt = ckpt["teacher"]
    renamed_checkpoint = {x[len("backbone."):]: y for x, y in ckpt.items() if x.startswith("backbone.")}
    msg = model.load_state_dict(renamed_checkpoint, strict=False)
    print(f"Loaded model with msg: {msg}")
    model.cuda()
    # model.eval()
    print(f"Model {args.arch} {args.patch_size}x{args.patch_size} built.")
    # load weights to evaluate

    # linear_classifier = LinearClassifier(model_embed_dim * (args.n_last_blocks + int(args.avgpool_patchtokens)),
    #                                      num_labels=args.num_labels)
    # linear_classifier = linear_classifier.cuda()
    # linear_classifier = nn.parallel.DistributedDataParallel(linear_classifier, device_ids=[args.gpu])

    # if args.lc_pretrained_weights:
    #     lc_ckpt = torch.load(args.lc_pretrained_weights)
    #     msg = linear_classifier.load_state_dict(lc_ckpt['state_dict'])
    #     print(f"Loaded linear classifier weights with msg: {msg}")
    #     test_stats = validate_network_multi_view(multi_crop_val_loader, model, linear_classifier, args.n_last_blocks,
    #                                              args.avgpool_patchtokens, config)
    #     # test_stats = validate_network(val_loader, model, linear_classifier, args.n_last_blocks, args.avgpool_patchtokens)
    #     print(test_stats)
    #     return True


    # set optimizer
    # optimizer = torch.optim.SGD(
    #     linear_classifier.parameters(),
    #     args.lr * (args.batch_size_per_gpu * utils.get_world_size()) / 256., # linear scaling rule
    #     momentum=0.9,
    #     weight_decay=0, # we do not apply weight decay
    # )
    
    optimizer = torch.optim.SGD(
        model.parameters(),
        args.lr * (args.batch_size_per_gpu * utils.get_world_size()) / 256., # linear scaling rule
        momentum=0.9,
        weight_decay=0.0001, # we apply weight decay for finetuning
    )
    
    # scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, args.epochs, eta_min=0)
    # scheduler for finetuning
    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[10,13], gamma=0.1) ###
    
    # Optionally resume from a checkpoint
    to_restore = {"epoch": 0, "best_acc": 0.}
    utils.restart_from_checkpoint(
        os.path.join(args.output_dir, "checkpoint.pth.tar"),
        run_variables=to_restore,
        state_dict=model,         ### chpt에서 linear classifier의 state_dict 불러 옴
        optimizer=optimizer,
        scheduler=scheduler,
    )
    start_epoch = to_restore["epoch"]
    best_acc = to_restore["best_acc"]

    for epoch in range(start_epoch, args.epochs):
        train_loader.sampler.set_epoch(epoch)

        train_stats = train(model, optimizer, train_loader, epoch, args.n_last_blocks, args.avgpool_patchtokens)
        scheduler.step()

        log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                     'epoch': epoch}
        if epoch % args.val_freq == 0 or epoch == args.epochs - 1:
            test_stats = validate_network(val_loader, model, args.n_last_blocks, args.avgpool_patchtokens)
            print(f"Accuracy at epoch {epoch} of the network on the {len(dataset_val)} test images: {test_stats['acc1']:.1f}%")
            best_acc = max(best_acc, test_stats["acc1"])
            print(f'Max accuracy so far: {best_acc:.2f}%')
            log_stats = {**{k: v for k, v in log_stats.items()},
                         **{f'test_{k}': v for k, v in test_stats.items()}}
        if utils.is_main_process():
            with (Path(args.output_dir) / "log.txt").open("a") as f:
                f.write(json.dumps(log_stats) + "\n")
            save_dict = {
                "epoch": epoch + 1,
                "state_dict": model.state_dict(),
                "optimizer": optimizer.state_dict(),
                "scheduler": scheduler.state_dict(),
                "best_acc": best_acc,
            }
            torch.save(save_dict, os.path.join(args.output_dir, "checkpoint.pth.tar"))

    test_stats = validate_network_multi_view(multi_crop_val_loader, model, args.n_last_blocks,
                                             args.avgpool_patchtokens, config)
    print(test_stats)

    print("Training of the supervised linear classifier on frozen features completed.\n"
          "Top-1 test accuracy: {acc:.1f}".format(acc=best_acc))


# def train(model, linear_classifier, optimizer, loader, epoch, n, avgpool):
#     model.train()
#     linear_classifier.train()
#     metric_logger = utils.MetricLogger(delimiter="  ")
#     metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
#     header = 'Epoch: [{}]'.format(epoch)
#     for (inp, target, sample_idx, meta) in metric_logger.log_every(loader, 20, header):
#         # move to gpu
#         inp = inp.cuda(non_blocking=True)
#         target = target.cuda(non_blocking=True)

#         # # forward
#         # with torch.no_grad():
#         #     # intermediate_output = model.get_intermediate_layers(inp, n)
#         #     # output = [x[:, 0] for x in intermediate_output]
#         #     # if avgpool:
#         #     #     output.append(torch.mean(intermediate_output[-1][:, 1:], dim=1))
#         #     # output = torch.cat(output, dim=-1)

#         #     output = model(inp)

#         output = linear_classifier(output)

#         # compute cross entropy loss
#         loss = nn.CrossEntropyLoss()(output, target)

#         # compute the gradients
#         optimizer.zero_grad()
#         loss.backward()

#         # step
#         optimizer.step()

#         # log
#         torch.cuda.synchronize()
#         metric_logger.update(loss=loss.item())
#         metric_logger.update(lr=optimizer.param_groups[0]["lr"])
#     # gather the stats from all processes
#     metric_logger.synchronize_between_processes()
#     print("Averaged stats:", metric_logger)
#     return {k: meter.global_avg for k, meter in metric_logger.meters.items()}

def train(model, optimizer, loader, epoch, n, avgpool):
    model.train()
    # linear_classifier.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)
    for (inp, target, sample_idx, meta) in metric_logger.log_every(loader, 20, header):
        # move to gpu
        inp = inp.cuda(non_blocking=True)
        target = target.cuda(non_blocking=True)

        # forward
        # with torch.no_grad():
        #     # intermediate_output = model.get_intermediate_layers(inp, n)
        #     # output = [x[:, 0] for x in intermediate_output]
        #     # if avgpool:
        #     #     output.append(torch.mean(intermediate_output[-1][:, 1:], dim=1))
        #     # output = torch.cat(output, dim=-1)

        output = model(inp)

        # output = linear_classifier(output)

        # compute cross entropy loss
        loss = nn.CrossEntropyLoss()(output, target)

        # compute the gradients
        optimizer.zero_grad()
        loss.backward()

        # step
        optimizer.step()

        # log
        torch.cuda.synchronize()
        metric_logger.update(loss=loss.item())
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])
    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}


# @torch.no_grad()
# def validate_network(val_loader, model, linear_classifier, n, avgpool):
#     linear_classifier.eval()
#     metric_logger = utils.MetricLogger(delimiter="  ")
#     header = 'Test:'
#     for (inp, target, sample_idx, meta) in metric_logger.log_every(val_loader, 20, header):
#         # move to gpu
#         inp = inp.cuda(non_blocking=True)
#         target = target.cuda(non_blocking=True)

#         # forward
#         with torch.no_grad():
#             # intermediate_output = model.get_intermediate_layers(inp, n)
#             # output = [x[:, 0] for x in intermediate_output]
#             # if avgpool:
#             #     output.append(torch.mean(intermediate_output[-1][:, 1:], dim=1))
#             # output = torch.cat(output, dim=-1)
#             output = model(inp)
#         output = linear_classifier(output)
#         loss = nn.CrossEntropyLoss()(output, target)

#         if linear_classifier.module.num_labels >= 5:
#             acc1, acc5 = utils.accuracy(output, target, topk=(1, 5))
#         else:
#             acc1, = utils.accuracy(output, target, topk=(1,))

#         batch_size = inp.shape[0]
#         metric_logger.update(loss=loss.item())
#         metric_logger.meters['acc1'].update(acc1.item(), n=batch_size)
#         if linear_classifier.module.num_labels >= 5:
#             metric_logger.meters['acc5'].update(acc5.item(), n=batch_size)
#     if linear_classifier.module.num_labels >= 5:
#         print('* Acc@1 {top1.global_avg:.3f} Acc@5 {top5.global_avg:.3f} loss {losses.global_avg:.3f}'
#               .format(top1=metric_logger.acc1, top5=metric_logger.acc5, losses=metric_logger.loss))
#     else:
#         print('* Acc@1 {top1.global_avg:.3f} loss {losses.global_avg:.3f}'
#               .format(top1=metric_logger.acc1, losses=metric_logger.loss))
#     return {k: meter.global_avg for k, meter in metric_logger.meters.items()}

@torch.no_grad()
def validate_network(val_loader, model, n, avgpool):
    # linear_classifier.eval()
    model.eval()
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'
    for (inp, target, sample_idx, meta) in metric_logger.log_every(val_loader, 20, header):
        # move to gpu
        inp = inp.cuda(non_blocking=True)
        target = target.cuda(non_blocking=True)

        # forward
        with torch.no_grad():
            # intermediate_output = model.get_intermediate_layers(inp, n)
            # output = [x[:, 0] for x in intermediate_output]
            # if avgpool:
            #     output.append(torch.mean(intermediate_output[-1][:, 1:], dim=1))
            # output = torch.cat(output, dim=-1)
            output = model(inp)
        # output = linear_classifier(output)
        loss = nn.CrossEntropyLoss()(output, target)

        # if linear_classifier.module.num_labels >= 5:
        #     acc1, acc5 = utils.accuracy(output, target, topk=(1, 5))
        # else:
        #     acc1, = utils.accuracy(output, target, topk=(1,))
            
        acc1, = utils.accuracy(output, target, topk=(1,))
        

        batch_size = inp.shape[0]
        metric_logger.update(loss=loss.item())
        metric_logger.meters['acc1'].update(acc1.item(), n=batch_size)
        # if linear_classifier.module.num_labels >= 5:
            # metric_logger.meters['acc5'].update(acc5.item(), n=batch_size)
    # if linear_classifier.module.num_labels >= 5:
        # print('* Acc@1 {top1.global_avg:.3f} Acc@5 {top5.global_avg:.3f} loss {losses.global_avg:.3f}'
        #       .format(top1=metric_logger.acc1, top5=metric_logger.acc5, losses=metric_logger.loss))
    # else:
    print('* Acc@1 {top1.global_avg:.3f} loss {losses.global_avg:.3f}'
            .format(top1=metric_logger.acc1, losses=metric_logger.loss))
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}


@torch.no_grad()
def validate_network_multi_view(val_loader, model, n, avgpool, cfg):
    # linear_classifier.eval()
    test_meter = TestMeter(
        len(val_loader.dataset)
        // (cfg.TEST.NUM_ENSEMBLE_VIEWS * cfg.TEST.NUM_SPATIAL_CROPS),
        cfg.TEST.NUM_ENSEMBLE_VIEWS * cfg.TEST.NUM_SPATIAL_CROPS,
        args.num_labels,
        len(val_loader),
        cfg.DATA.MULTI_LABEL,
        cfg.DATA.ENSEMBLE_METHOD,
        )
    test_meter.iter_tic()

    for cur_iter, (inp, target, sample_idx, meta) in tqdm(enumerate(val_loader), total=len(val_loader)):
        # move to gpu
        inp = inp.cuda(non_blocking=True)
        # target = target.cuda(non_blocking=True)
        test_meter.data_toc()

        # forward
        with torch.no_grad():
            output = model(inp)
        # output = linear_classifier(output)

        output = output.cpu()
        target = target.cpu()
        sample_idx = sample_idx.cpu()

        test_meter.iter_toc()
        # Update and log stats.
        test_meter.update_stats(
            output.detach(), target.detach(), sample_idx.detach()
        )
        test_meter.log_iter_stats(cur_iter)

        test_meter.iter_tic()

    test_meter.finalize_metrics()
    return test_meter.stats


# class LinearClassifier(nn.Module):
#     """Linear layer to train on top of frozen features"""
#     def __init__(self, dim, num_labels=1000):
#         super(LinearClassifier, self).__init__()
#         self.num_labels = num_labels
#         self.linear = nn.Linear(dim, num_labels)
#         self.linear.weight.data.normal_(mean=0.0, std=0.01)
#         self.linear.bias.data.zero_()

#     def forward(self, x):
#         # flatten
#         x = x.view(x.size(0), -1)

#         # linear layer
#         return self.linear(x)


if __name__ == '__main__':
    parser = argparse.ArgumentParser('Evaluation with linear classification on ImageNet')
    parser.add_argument('--n_last_blocks', default=4, type=int, help="""Concatenate [CLS] tokens
        for the `n` last blocks. We use `n=4` when evaluating ViT-Small and `n=1` with ViT-Base.""")
    parser.add_argument('--avgpool_patchtokens', default=False, type=utils.bool_flag,
                        help="""Whether ot not to concatenate the global average pooled features to the [CLS] token.
        We typically set this to False for ViT-Small and to True with ViT-Base.""")
    parser.add_argument('--arch', default='vit_small', type=str,
                        choices=['vit_tiny', 'vit_small', 'vit_base', 'swin'],
                        help='Architecture (support only ViT atm).')
    parser.add_argument('--patch_size', default=16, type=int, help='Patch resolution of the model.')
    parser.add_argument('--pretrained_weights', default='', type=str, help="Path to pretrained weights to evaluate.")
    parser.add_argument('--lc_pretrained_weights', default='', type=str, help="Path to pretrained weights to evaluate.")
    parser.add_argument("--checkpoint_key", default="teacher", type=str, help='Key to use in the checkpoint (example: "teacher")')
    parser.add_argument('--epochs', default=100, type=int, help='Number of epochs of training.')
    parser.add_argument("--lr", default=0.001, type=float, help="""Learning rate at the beginning of
        training (highest LR used during training). The learning rate is linearly scaled
        with the batch size, and specified here for a reference batch size of 256.
        We recommend tweaking the LR depending on the checkpoint evaluated.""")
    parser.add_argument('--batch_size_per_gpu', default=128, type=int, help='Per-GPU batch-size')
    parser.add_argument("--dist_url", default="env://", type=str, help="""url used to set up
        distributed training; see https://pytorch.org/docs/stable/distributed.html""")
    parser.add_argument("--local_rank", default=0, type=int, help="Please ignore and do not set this argument.")
    parser.add_argument('--data_path', default='/path/to/imagenet/', type=str)
    parser.add_argument('--num_workers', default=10, type=int, help='Number of data loading workers per GPU.')
    parser.add_argument('--val_freq', default=1, type=int, help="Epoch frequency for validation.")
    parser.add_argument('--output_dir', default=".", help='Path to save logs and checkpoints')
    parser.add_argument('--num_labels', default=1000, type=int, help='Number of labels for linear classifier')
    parser.add_argument('--dataset', default="ucf101", help='Dataset: ucf101 / hmdb51')
    parser.add_argument('--use_flow', default=False, type=utils.bool_flag, help="use flow teacher")

    # config file
    parser.add_argument("--cfg", dest="cfg_file", help="Path to the config file", type=str,
                        default="models/configs/Kinetics/TimeSformer_divST_8x32_224.yaml")
    parser.add_argument("--opts", help="See utils/defaults.py for all options", default=None, nargs=argparse.REMAINDER)

    args = parser.parse_args()
    eval_linear(args)
