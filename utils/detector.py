"""
ok so I lied. it's not a detector, it's the resnet backbone
"""

import torch
import torch.nn as nn
import torch.nn.parallel
from torchvision.models import resnet

from utils.pytorch_misc import Flattener
from torchvision.ops import RoIAlign
import torch.utils.model_zoo as model_zoo
from config import USE_IMAGENET_PRETRAINED
from utils.pytorch_misc import pad_sequence
from torch.nn import functional as F
from utils.cvm import RegionCVM


def _load_resnet(pretrained=True):
    # huge thx to https://github.com/ruotianluo/pytorch-faster-rcnn/blob/master/lib/nets/resnet_v1.py
    backbone = resnet.resnet50(pretrained=False)
    if pretrained:
        backbone.load_state_dict(model_zoo.load_url(
            'https://s3.us-west-2.amazonaws.com/ai2-rowanz/resnet50-e13db6895d81.th'))
    for i in range(2, 4):
        getattr(backbone, 'layer%d' % i)[0].conv1.stride = (2, 2)
        getattr(backbone, 'layer%d' % i)[0].conv2.stride = (1, 1)
    return backbone


def _load_resnet_imagenet(pretrained=True):
    # huge thx to https://github.com/ruotianluo/pytorch-faster-rcnn/blob/master/lib/nets/resnet_v1.py
    backbone = resnet.resnet50(pretrained=pretrained)
    for i in range(2, 4):
        getattr(backbone, 'layer%d' % i)[0].conv1.stride = (2, 2)
        getattr(backbone, 'layer%d' % i)[0].conv2.stride = (1, 1)
    # use stride 1 for the last conv4 layer (same as tf-faster-rcnn)
    backbone.layer4[0].conv2.stride = (1, 1)
    backbone.layer4[0].downsample[0].stride = (1, 1)

    # # Make batchnorm more sensible
    # for submodule in backbone.modules():
    #     if isinstance(submodule, torch.nn.BatchNorm2d):
    #         submodule.momentum = 0.01

    return backbone


class SimpleDetector(nn.Module):
    def __init__(self, pretrained=True, average_pool=True, semantic=True, final_dim=1024, layer_fix=True):
        """
        :param average_pool: whether or not to average pool the representations
        :param pretrained: Whether we need to load from scratch
        :param semantic: Whether or not we want to introduce the mask and the class label early on (default Yes)
        """
        super(SimpleDetector, self).__init__()
        # huge thx to https://github.com/ruotianluo/pytorch-faster-rcnn/blob/master/lib/nets/resnet_v1.py
        backbone = _load_resnet_imagenet(pretrained=pretrained) if USE_IMAGENET_PRETRAINED else _load_resnet(
            pretrained=pretrained)
        self.pre_backbone = nn.Sequential(
            backbone.conv1,
            backbone.bn1,
            backbone.relu,
            backbone.maxpool,
            backbone.layer1,
        )
        self.layer2 = backbone.layer2
        self.cvm_2 = RegionCVM(in_channels=128 * 4, grid=[6, 6])
        self.layer3 = backbone.layer3
        self.cvm_3 = RegionCVM(in_channels=256 * 4, grid=[4, 4])
        self.roi_align = RoIAlign((7, 7) if USE_IMAGENET_PRETRAINED else (14, 14),
                                  spatial_scale=1 / 16, sampling_ratio=0)
        if semantic:
            self.mask_dims = 32
            self.object_embed = torch.nn.Embedding(num_embeddings=81, embedding_dim=128)
            self.mask_upsample = torch.nn.Conv2d(1, self.mask_dims, kernel_size=3,
                                                 stride=2 if USE_IMAGENET_PRETRAINED else 1,
                                                 padding=1, bias=True)
        else:
            self.object_embed = None
            self.mask_upsample = None

        self.layer4 = backbone.layer4
        self.cvm_4 = RegionCVM(in_channels=512 * 4, grid=[1, 1])
        after_roi_align = []

        self.final_dim = final_dim
        if average_pool:
            after_roi_align += [nn.AvgPool2d(7, stride=1), Flattener()]

        self.after_roi_align = torch.nn.Sequential(*after_roi_align)

        self.obj_downsample = torch.nn.Sequential(
            torch.nn.Dropout(p=0.1),
            torch.nn.Linear(2048 + (128 if semantic else 0), final_dim),
            torch.nn.ReLU(inplace=True),
        )
        self.regularizing_predictor = torch.nn.Linear(2048, 81)

        for m in self.pre_backbone.modules():
            for p in m.parameters():
                p.requires_grad = False

        def set_bn_fix(m):
            classname = m.__class__.__name__
            if classname.find('BatchNorm') != -1:
                for p in m.parameters(): p.requires_grad = False

        self.layer2.apply(set_bn_fix)
        self.layer3.apply(set_bn_fix)
        self.layer4.apply(set_bn_fix)
        if layer_fix:
            for m in self.layer2.modules():
                for p in m.parameters():
                    p.requires_grad = False
            for m in self.layer3.modules():
                for p in m.parameters():
                    p.requires_grad = False
            for m in self.layer4.modules():
                for p in m.parameters():
                    p.requires_grad = False

    def forward(self,
                images: torch.Tensor,
                boxes: torch.Tensor,
                box_mask: torch.LongTensor,
                classes: torch.Tensor = None,
                segms: torch.Tensor = None,
                ):
        """
        :param images: [batch_size, 3, im_height, im_width]
        :param boxes:  [batch_size, max_num_objects, 4] Padded boxes
        :param box_mask: [batch_size, max_num_objects] Mask for whether or not each box is OK
        :return: object reps [batch_size, max_num_objects, dim]
        """

        images = self.pre_backbone(images)
        images = self.layer2(images)
        images = self.cvm_2(images)
        images = self.layer3(images)
        images = self.cvm_3(images)
        images = self.layer4(images)
        img_feats = self.cvm_4(images)
        box_inds = box_mask.nonzero()
        assert box_inds.shape[0] > 0
        rois = torch.cat((
            box_inds[:, 0, None].type(boxes.dtype),
            boxes[box_inds[:, 0], box_inds[:, 1]],
        ), 1)
        rois = rois.type(torch.cuda.FloatTensor)

        # Object class and segmentation representations
        roi_align_res = self.roi_align(img_feats, rois)
        if self.mask_upsample is not None:
            assert segms is not None
            segms_indexed = segms[box_inds[:, 0], None, box_inds[:, 1]] - 0.5
            roi_align_res[:, :self.mask_dims] += self.mask_upsample(segms_indexed)

        post_RoIAlign = self.after_roi_align(roi_align_res)

        # Add some regularization, encouraging the model to keep giving decent enough predictions
        obj_logits = self.regularizing_predictor(post_RoIAlign)
        obj_labels = classes[box_inds[:, 0], box_inds[:, 1]]
        cnn_regularization = F.cross_entropy(obj_logits, obj_labels, reduction='mean')[None]

        feats_to_downsample = post_RoIAlign if self.object_embed is None else torch.cat(
            (post_RoIAlign, self.object_embed(obj_labels)), -1)
        roi_aligned_feats = self.obj_downsample(feats_to_downsample)

        # Reshape into a padded sequence - this is expensive and annoying but easier to implement and debug...
        obj_reps = pad_sequence(roi_aligned_feats, box_mask.sum(1).tolist())
        return {
            'obj_reps_raw': post_RoIAlign,
            'obj_reps': obj_reps,
            'obj_logits': obj_logits,
            'obj_labels': obj_labels,
            'cnn_regularization_loss': cnn_regularization
        }
