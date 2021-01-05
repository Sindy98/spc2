# from ..utils.transform import resize, random_crop, random_flip, center_crop, meshgrid, box_iou, box_nms, change_box_order
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import math
import torch.nn.init as init


def one_hot_embedding(labels, num_classes):
    '''Embedding labels to one-hot form.

    Args:
      labels: (LongTensor) class labels, sized [N,].
      num_classes: (int) number of classes.

    Returns:
      (tensor) encoded labels, sized [N,#classes].
    '''
    # import pdb; pdb.set_trace()
    y = torch.eye(num_classes)  # [D,D]
    return y[labels.long()]            # [N,D]


class FocalLoss(nn.Module):
    def __init__(self, num_classes=2, eps=1e-7):
        super(FocalLoss, self).__init__()
        self.num_classes = num_classes
        self.eps = eps
        self.loss_total = 0
        self.round = 0

    def focal_loss(self, x, y):
        '''Focal loss.

        Args:
          x: (tensor) sized [N,D].
          y: (tensor) sized [N,].

        Return:
          (tensor) focal loss.
        '''
        alpha = 0.25
        gamma = 2

        t = one_hot_embedding(y.data.cpu(), 1+self.num_classes)  # [N,21]
        t = t[:,1:]  # exclude background
        t = Variable(t).cuda()  # [N,20]

        p = x.sigmoid()
        pt = p*t + (1-p)*(1-t)         # pt = p if t > 0 else 1-p
        w = alpha*t + (1-alpha)*(1-t)  # w = alpha if t > 0 else 1-alpha
        w = w * (1-pt).pow(gamma)
        return F.binary_cross_entropy_with_logits(x, t, w, reduction='sum')

    def focal_loss_alt(self, x, y):
        '''Focal loss alternative.

        Args:
          x: (tensor) sized [N,D].
          y: (tensor) sized [N,].

        Return:
          (tensor) focal loss.
        '''
        alpha = 0.25
        t = one_hot_embedding(y.data.cpu(), 1 + self.num_classes)
        t = t[:, 1:]
        t = Variable(t).cuda()
        xt = x * (2 * t - 1)  # xt = x if t > 0 else -x
        pt = (2 * xt + 1).sigmoid()
        w = alpha * t + (1-alpha) * (1-t)
        logit = pt.clamp(self.eps, 1.-self.eps)
        loss = -w * logit.log() / 2

        return loss.sum()

    def forward(self, loc_preds, loc_targets, cls_preds, cls_targets):
        '''Compute loss between (loc_preds, loc_targets) and (cls_preds, cls_targets).

        Args:
          loc_preds: (tensor) predicted locations, sized [batch_size, #anchors, 4].
          loc_targets: (tensor) encoded target locations, sized [batch_size, #anchors, 4].
          cls_preds: (tensor) predicted class confidences, sized [batch_size, #anchors, #classes].
          cls_targets: (tensor) encoded target labels, sized [batch_size, #anchors].

        loss:
          (tensor) loss = SmoothL1Loss(loc_preds, loc_targets) + FocalLoss(cls_preds, cls_targets).
        '''
        batch_size, num_boxes = cls_targets.size()
        pos = cls_targets > 0  # [N,#anchors]
        num_pos = pos.data.long().sum()
        num_pos = max(1.0, num_pos.item())

        ################################################################
        # loc_loss = SmoothL1Loss(pos_loc_preds, pos_loc_targets)
        ################################################################
        mask = pos.unsqueeze(2).expand_as(loc_preds)       # [N,#anchors,4]
        masked_loc_preds = loc_preds[mask].view(-1,4)      # [#pos,4]
        masked_loc_targets = loc_targets[mask].view(-1,4)  # [#pos,4]

        loc_loss = F.smooth_l1_loss(masked_loc_preds, masked_loc_targets, reduction='sum')

        ################################################################
        # cls_loss = FocalLoss(loc_preds, loc_targets)
        ################################################################
        
        pos_neg = cls_targets > -1  # exclude ignored anchors
        # num_peg = pos_neg.data.long().sum()
        mask = pos_neg.unsqueeze(2).expand_as(cls_preds)
        
        masked_cls_preds = cls_preds[mask].view(-1,self.num_classes)

        cls_loss = self.focal_loss_alt(masked_cls_preds, cls_targets[pos_neg])

        loc_loss /= num_pos
        cls_loss /= num_pos

        loss = loc_loss + cls_loss
        loss = loss / batch_size
        self.round += 1
        self.loss_total += loss.item()

        isnan = torch.isnan(loss).max() == 1
        print(torch.isnan(loss))
        if isnan:
            import pdb; pdb.set_trace()
        print('loc_loss: %.4f | cls_loss: %.4f | avg_loss: %.4f' % (loc_loss.item()/batch_size, cls_loss.item()/batch_size, self.loss_total/self.round))

        return loss

class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, in_planes, planes, stride=1):
        super(Bottleneck, self).__init__()
        self.conv1 = nn.Conv2d(in_planes, planes, kernel_size=1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=stride, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.conv3 = nn.Conv2d(planes, self.expansion*planes, kernel_size=1, bias=False)
        self.bn3 = nn.BatchNorm2d(self.expansion*planes)

        self.downsample = nn.Sequential()
        if stride != 1 or in_planes != self.expansion*planes:
            self.downsample = nn.Sequential(
                nn.Conv2d(in_planes, self.expansion*planes, kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(self.expansion*planes)
            )

    def forward(self, x):
        out = F.relu(self.bn1(self.conv1(x)))
        out = F.relu(self.bn2(self.conv2(out)))
        out = self.bn3(self.conv3(out))
        out += self.downsample(x)
        out = F.relu(out)
        return out


class FPN(nn.Module):
    def __init__(self, block, num_blocks):
        super(FPN, self).__init__()
        self.in_planes = 64

        self.conv1 = nn.Conv2d(3, 64, kernel_size=7, stride=2, padding=3, bias=False)
        self.bn1 = nn.BatchNorm2d(64)

        # Bottom-up layers
        self.layer1 = self._make_layer(block,  64, num_blocks[0], stride=1)
        self.layer2 = self._make_layer(block, 128, num_blocks[1], stride=2)
        self.layer3 = self._make_layer(block, 256, num_blocks[2], stride=2)
        self.layer4 = self._make_layer(block, 512, num_blocks[3], stride=2)
        self.conv6 = nn.Conv2d(2048, 256, kernel_size=3, stride=2, padding=1)
        self.conv7 = nn.Conv2d( 256, 256, kernel_size=3, stride=2, padding=1)

        # Lateral layers
        self.latlayer1 = nn.Conv2d(2048, 256, kernel_size=1, stride=1, padding=0)
        self.latlayer2 = nn.Conv2d(1024, 256, kernel_size=1, stride=1, padding=0)
        self.latlayer3 = nn.Conv2d( 512, 256, kernel_size=1, stride=1, padding=0)

        # Top-down layers
        self.toplayer1 = nn.Conv2d(256, 256, kernel_size=3, stride=1, padding=1)
        self.toplayer2 = nn.Conv2d(256, 256, kernel_size=3, stride=1, padding=1)

    def _make_layer(self, block, planes, num_blocks, stride):
        strides = [stride] + [1]*(num_blocks-1)
        layers = []
        for stride in strides:
            layers.append(block(self.in_planes, planes, stride))
            self.in_planes = planes * block.expansion
        return nn.Sequential(*layers)

    def _upsample_add(self, x, y):
        '''Upsample and add two feature maps.

        Args:
          x: (Variable) top feature map to be upsampled.
          y: (Variable) lateral feature map.

        Returns:
          (Variable) added feature map.

        Note in PyTorch, when input size is odd, the upsampled feature map
        with `F.upsample(..., scale_factor=2, mode='nearest')`
        maybe not equal to the lateral feature map size.

        e.g.
        original input size: [N,_,15,15] ->
        conv2d feature map size: [N,_,8,8] ->
        upsampled feature map size: [N,_,16,16]

        So we choose bilinear upsample which supports arbitrary output sizes.
        '''
        _,_,H,W = y.size()
        return F.upsample(x, size=(H,W), mode='bilinear') + y

    def forward(self, x):
        # Bottom-up
        c1 = F.relu(self.bn1(self.conv1(x)))
        c1 = F.max_pool2d(c1, kernel_size=3, stride=2, padding=1)
        c2 = self.layer1(c1)
        c3 = self.layer2(c2)
        c4 = self.layer3(c3)
        c5 = self.layer4(c4)
        p6 = self.conv6(c5)
        p7 = self.conv7(F.relu(p6))
        # Top-down
        p5 = self.latlayer1(c5)
        p4 = self._upsample_add(p5, self.latlayer2(c4))
        p4 = self.toplayer1(p4)
        p3 = self._upsample_add(p4, self.latlayer3(c3))
        p3 = self.toplayer2(p3)
        return p3, p4, p5, p6, p7


def FPN50():
    return FPN(Bottleneck, [3,4,6,3])


class RetinaNet_FPN(nn.Module):
    num_anchors = 9
    
    def __init__(self, num_classes=2):
        super(RetinaNet_FPN, self).__init__()
        self.fpn = FPN50()
        self.num_classes = num_classes
        self.loc_head = self._make_head(self.num_anchors * 4)
        self.cls_head = self._make_head(self.num_anchors * self.num_classes)

    def forward(self, x):
        fms = self.fpn(x)
        loc_preds = []
        cls_preds = []
        for fm in fms:
            loc_pred = self.loc_head(fm)
            cls_pred = self.cls_head(fm)
            loc_pred = loc_pred.permute(0,2,3,1).contiguous().view(x.size(0),-1,4)                 # [N, 9*4,H,W] -> [N,H,W, 9*4] -> [N,H*W*9, 4]
            cls_pred = cls_pred.permute(0,2,3,1).contiguous().view(x.size(0),-1,self.num_classes)  # [N,9*20,H,W] -> [N,H,W,9*20] -> [N,H*W*9,20]
            loc_preds.append(loc_pred)
            cls_preds.append(cls_pred)
        return torch.cat(loc_preds, 1), torch.cat(cls_preds, 1)

    def _make_head(self, out_planes):
        layers = []
        for _ in range(4):
            layers.append(nn.Conv2d(256, 256, kernel_size=3, stride=1, padding=1))
            layers.append(nn.ReLU(True))
        layers.append(nn.Conv2d(256, out_planes, kernel_size=3, stride=1, padding=1))
        return nn.Sequential(*layers)

    def freeze_bn(self):
        '''Freeze BatchNorm layers.'''
        for layer in self.modules():
            if isinstance(layer, nn.BatchNorm2d):
                layer.eval()


class RetinaNet(nn.Module):
    num_anchors = 9
    
    def __init__(self, num_classes=2, channel=256):
        super(RetinaNet, self).__init__()
        self.channel = channel
        self.num_classes = num_classes
        self.loc_head = self._make_head(self.num_anchors*4) # predict bbox location and corresponding rotations
        self.ori_head = self._make_head(self.num_anchors)
        self.cls_head = self._make_head(self.num_anchors*self.num_classes)
        self.coll_head = self._make_head(self.num_anchors*2)

    def init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                init.normal(m.weight, mean=0, std=0.01)
                if m.bias is not None:
                    init.constant(m.bias, 0)
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

        pi = 0.01
        init.constant(self.cls_head[-1].bias, -math.log((1-pi)/pi))

    def forward(self, fms, training=True):
        '''
        fms: feature maps extracted from input observations
        for image width is w and height is h, fms should be:
        fms[0]: channel x (H/4) x (W/4)
        fms[1]: channel x (H/8) x (W/8)
        fms[2]: channel x (H/16) x (W/16)
        fms[3]: channel x (H/32) x (W/32) 
        '''
        loc_preds = []
        cls_preds = []
        coll_preds = []
        ori_preds = []
        for fm in fms:
            loc_pred = self.loc_head(fm)
            cls_pred = self.cls_head(fm)
            coll_pred = self.coll_head(fm)
            ori_pred = self.ori_head(fm)
            loc_pred = loc_pred.permute(0,2,3,1).contiguous().view(fm.size(0),-1,4)                 # [N, 9*4,H,W] -> [N,H,W, 9*4] -> [N,H*W*9, 4]
            cls_pred = cls_pred.permute(0,2,3,1).contiguous().view(fm.size(0),-1,self.num_classes)  # [N,9*20,H,W] -> [N,H,W,9*20] -> [N,H*W*9,20]
            coll_pred = coll_pred.permute(0,2,3,1).contiguous().view(fm.size(0),-1,2)
            ori_pred = ori_pred.permute(0,2,3,1).contiguous().view(fm.size(0),-1,1)
            loc_preds.append(loc_pred)
            cls_preds.append(cls_pred)
            coll_preds.append(coll_pred)
            ori_preds.append(ori_pred)
        
        return torch.cat(loc_preds, 1), torch.cat(cls_preds, 1), torch.cat(coll_preds, 1)
        
    def _make_head(self, out_planes):
        layers = []
        for _ in range(4):
            layers.append(nn.Conv2d(self.channel, self.channel, kernel_size=3, stride=1, padding=1))
            layers.append(nn.BatchNorm2d(self.channel))
            layers.append(nn.ReLU(True))
        layers.append(nn.Conv2d(self.channel, out_planes, kernel_size=3, stride=1, padding=1))
        return nn.Sequential(*layers)

    def freeze_bn(self):
        '''Freeze BatchNorm layers.'''
        for layer in self.modules():
            if isinstance(layer, nn.BatchNorm2d):
                layer.eval()
    
