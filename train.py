r"""PyTorch Detection Training.

To run in a multi-gpu environment, use the distributed launcher::

    python -m torch.distributed.launch --nproc_per_node=$NGPU --use_env \
        train.py ... --world-size $NGPU

"""
import datetime
import os
import time

import torch
import torch.utils.data
from torch import nn
import torchvision
import torchvision.models.detection
import torchvision.models.detection.mask_rcnn

from coco_utils import get_coco, get_coco_kp
from voc_utils import get_voc, get_custom_voc

from group_by_aspect_ratio import GroupedBatchSampler, create_aspect_ratio_groups
from engine import train_one_epoch, voc_evaluate, coco_evaluate, custom_voc_evaluate

import utils
import transforms as T


def get_dataset(name, image_set, transform, data_path):
    paths = {
        "coco": (data_path, get_coco, 91),
        "coco_kp": (data_path, get_coco_kp, 2),
        "voc": (data_path, get_voc, 21),
        "custom_voc": (data_path, get_custom_voc, 3)
    }
    p, ds_fn, num_classes = paths[name]

    if name == 'custom_voc':  # 加载自定义的Pascal格式数据集
        ds = ds_fn(p, transforms=transform)
        return ds, num_classes
    else:
        ds = ds_fn(p, image_set=image_set, transforms=transform)
        return ds, num_classes


def get_transform(train):
    transforms = []
    transforms.append(T.ToTensor())
    if train:
        transforms.append(T.RandomHorizontalFlip(0.5))
    return T.Compose(transforms)


def main(args):
    utils.init_distributed_mode(args)
    print(args)

    device = torch.device(args.device)

    # Data loading code
    print("Loading data")

    # 支持加载自定义Pascal格式数据集 参数dataset设置为custom_voc
    if args.dataset == 'custom_voc':
        # dataset, num_classes = get_custom_voc(args.train_data_path,get_transform(train=True)) 
        # dataset_test, _ = get_custom_voc(args.test_data_path,get_transform(train=False))

        # 如果是自定义Pascal数据集,不需要传入image_set参数,因此这里设置为None
        dataset, num_classes = get_dataset(args.dataset, None, get_transform(train=True), args.train_data_path)
        dataset_test, _ = get_dataset(args.dataset, None, get_transform(train=False), args.test_data_path)
    else:
        dataset, num_classes = get_dataset(args.dataset, "train" if args.dataset == 'coco' else 'trainval',
                                           get_transform(train=True), args.data_path)
        dataset_test, _ = get_dataset(args.dataset, "test" if args.dataset == 'coco' else 'val',
                                      get_transform(train=False), args.data_path)

    print("Creating data loaders")
    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(dataset)
        test_sampler = torch.utils.data.distributed.DistributedSampler(dataset_test)
    else:
        train_sampler = torch.utils.data.RandomSampler(dataset)
        test_sampler = torch.utils.data.SequentialSampler(dataset_test)

    if args.aspect_ratio_group_factor >= 0:
        group_ids = create_aspect_ratio_groups(dataset, k=args.aspect_ratio_group_factor)
        train_batch_sampler = GroupedBatchSampler(train_sampler, group_ids, args.batch_size)
    else:
        train_batch_sampler = torch.utils.data.BatchSampler(
            train_sampler, args.batch_size, drop_last=True)

    data_loader = torch.utils.data.DataLoader(
        dataset, batch_sampler=train_batch_sampler, num_workers=args.workers,
        collate_fn=utils.collate_fn)

    data_loader_test = torch.utils.data.DataLoader(
        dataset_test, batch_size=1,
        sampler=test_sampler, num_workers=args.workers,
        collate_fn=utils.collate_fn)

    print("Creating model")
    # model = torchvision.models.detection.fasterrcnn_resnet50_fpn()
    model = torchvision.models.detection.__dict__[args.model](num_classes=num_classes,
                                                              pretrained=args.pretrained)
    model.to(device)

    model_without_ddp = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        model_without_ddp = model.module

    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.SGD(
        params, lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)

    # lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=args.lr_step_size, gamma=args.lr_gamma)
    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=args.lr_steps, gamma=args.lr_gamma)

    if args.resume:
        checkpoint = torch.load(args.resume, map_location='cpu')
        model_without_ddp.load_state_dict(checkpoint['model'])
        optimizer.load_state_dict(checkpoint['optimizer'])  # 用于恢复训练,处理模型还需要优化器和学习率规则
        lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])

    # 如果只进行模型测试,注意这里传入的参数是--resume, 原作者只提到了--resume用于恢复训练,根据官方文档可知也是可以用于模型推理的
    # 参考官方文档https://pytorch.org/tutorials/beginner/saving_loading_models.html
    if args.test_only:
        if not args.resume:
            raise Exception('需要checkpoints模型用于推理!')
        else:
            checkpoint = torch.load(args.resume, map_location='cpu')
            model_without_ddp.load_state_dict(checkpoint['model'])

            if 'coco' == args.dataset:
                coco_evaluate(model_without_ddp, data_loader_test, device=device)
            elif 'voc' == args.dataset:
                voc_evaluate(model_without_ddp, data_loader_test, device=device)
            elif 'custom_voc' == args.dataset:
                custom_voc_evaluate(model_without_ddp, data_loader_test, device=device)
            else:
                print(f'No evaluation method available for the dataset {args.dataset}')
            # evaluate(model, data_loader_test, device=device)
            return

    print("Start training")
    start_time = time.time()
    for epoch in range(args.epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)
        train_one_epoch(model, optimizer, data_loader, device, epoch, args.print_freq)
        lr_scheduler.step()
        if args.output_dir:
            # model.save('./checkpoints/model_{}_{}.pth'.format(args.dataset, epoch))
            utils.save_on_master({
                'model': model_without_ddp.state_dict(),  # 存储网络参数(不存储网络骨架)
                # 'model': model_without_ddp, # 存储整个网络
                'optimizer': optimizer.state_dict(),
                'lr_scheduler': lr_scheduler.state_dict(),
                'args': args},
                os.path.join(args.output_dir, 'model_{}_{}.pth'.format(args.dataset, epoch)))

        # evaluate after every epoch
        if args.dataset == 'coco':
            coco_evaluate(model, data_loader_test, device=device)
        elif 'voc' == args.dataset:
            voc_evaluate(model, data_loader_test, device=device)
        elif 'custom_voc' == args.dataset:
            custom_voc_evaluate(model, data_loader_test, device=device)
        else:
            print(f'No evaluation method available for the dataset {args.dataset}')

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__)

    parser.add_argument('--data-path', default='./', help='dataset path used for coco and voc(default is "./")')
    parser.add_argument('--train-data-path', help='train dataset path for custom voc dataset')
    parser.add_argument('--test-data-path', help='test dataset path for custom voc dataset')
    parser.add_argument('--dataset', default='coco',
                        help='dataset type, option are "coco", "voc" and "coco_kp", defualt is "coco"')
    parser.add_argument('--model', default='fasterrcnn_resnet50_fpn', help='model, default="fasterrcnn_resnet50_fpn"')
    parser.add_argument('--device', default='cuda', help='device, default is cuda')
    parser.add_argument('-b', '--batch-size', default=2, type=int,
                        help='number of batch_size(default is 2)')
    parser.add_argument('--epochs', default=13, type=int, metavar='N',
                        help='number of total epochs to run(default is 13)')
    parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                        help='number of data loading workers (default: 16)')
    parser.add_argument('--lr', default=0.02, type=float, help='initial learning rate')
    parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                        help='momentum')
    parser.add_argument('--wd', '--weight-decay', default=1e-4, type=float,
                        metavar='W', help='weight decay (default: 1e-4)',
                        dest='weight_decay')
    parser.add_argument('--lr-step-size', default=8, type=int, help='decrease lr every step-size epochs')
    parser.add_argument('--lr-steps', default=[8, 11], nargs='+', type=int, help='decrease lr every step-size epochs')
    parser.add_argument('--lr-gamma', default=0.1, type=float, help='decrease lr by a factor of lr-gamma')
    parser.add_argument('--print-freq', default=20, type=int, help='print frequency')
    parser.add_argument('--output-dir', default='./', help='path where to save,default="./" ')
    parser.add_argument('--resume', default='', help='resume from checkpoint,default=''')
    parser.add_argument('--aspect-ratio-group-factor', default=0, type=int)
    parser.add_argument(
        "--test-only",
        dest="test_only",
        help="Only test the model",
        action="store_true",
    )
    parser.add_argument(
        "--pretrained",
        dest="pretrained",
        help="Use pre-trained models from the modelzoo",
        action="store_true",
    )

    # distributed training parameters
    parser.add_argument('--world-size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist-url', default='env://', help='url used to set up distributed training')

    args = parser.parse_args()

    if args.output_dir:
        utils.mkdir(args.output_dir)

    main(args)
