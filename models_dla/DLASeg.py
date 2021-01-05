import math
import numpy as np
import torch.nn as nn
import models.dla as dla
from models_dla.dla_up import DLAUp, Identity, fill_up_weights, BatchNorm
from utils.util import weights_init


# Backbone network for feature extraction
class DLASeg(nn.Module):
    def __init__(self, args, down_ratio=2):
        super(DLASeg, self).__init__()
        assert down_ratio in [2, 4, 8, 16]
        self.first_level = int(np.log2(down_ratio))
        self.base = dla.__dict__[args.drn_model](pretrained=args.pretrained, return_levels=True)
        channels = self.base.channels
        scales = [2 ** i for i in range(len(channels[self.first_level:]))]
        self.dla_up = DLAUp(channels[self.first_level:], scales=scales)
        self.fc = nn.Sequential(
            nn.Conv2d(channels[self.first_level], args.classes, kernel_size=1,
                      stride=1, padding=0, bias=True)
        )
        up_factor = 2 ** self.first_level
        if up_factor == 4:
            up = nn.Sequential(
                nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
                nn.Conv2d(args.classes, args.classes, 3, padding=1),
                nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
                nn.Conv2d(args.classes, args.classes, 5, padding=2)
            )
            up.apply(weights_init)
        elif up_factor > 1:
            up = nn.ConvTranspose2d(args.classes, args.classes, up_factor * 2,
                                    stride=up_factor, padding=up_factor // 2,
                                    output_padding=0, groups=args.classes,
                                    bias=False)
            fill_up_weights(up)
            up.weight.requires_grad = False
        else:
            up = Identity()
        self.up = up
        self.softmax = nn.Softmax(dim=1)
        self.logsoftmax = nn.LogSoftmax(dim=1)
        
        self.layer0 = self.make_seq(64, 64)
        self.layer1 = self.make_seq(64, 64)
        self.layer2 = self.make_seq(128, 128)
        self.layer3 = self.make_seq(256, 256)

        for m in self.fc.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
            elif isinstance(m, BatchNorm):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    def make_seq(self, in_channel, out_channel, kz=1, stride=1, bias=True):
        return nn.Sequential(
            nn.Conv2d(in_channel, out_channel, kernel_size=1, stride=1, bias=bias),
            nn.GroupNorm(out_channel, out_channel),
            nn.ReLU(True)
        )

    def norm(self, x):
        x[0] = self.layer0(x[0])
        x[1] = self.layer1(x[1])
        x[2] = self.layer2(x[2])
        x[3] = self.layer3(x[3])
        return x

    def forward(self, x, train=True):
        x = self.base(x)[self.first_level:]
        x = self.norm(x)
        xx = x
        if train:
            # x, y, out_fms = self.infer(x)
            # return xx, x, y, out_fms
            out_fms = self.dla_up(x)
            x = out_fms[-1]
            x = self.fc(x)
            x = self.up(x)
            y = self.logsoftmax(x)
            x = self.softmax(x)
            return xx, x, y, [xx[-1]] + out_fms
        else:
            return xx

    def infer(self, x):
        x = self.norm(x)
        xx = x
        out_fms = self.dla_up(x)
        x = out_fms[-1]
        x = self.fc(x)
        x = self.up(x)
        y = self.logsoftmax(x)
        x = self.softmax(x)
        return x, y, [xx[-1]] + out_fms

    def optim_parameters(self):
        for param in self.base.parameters():
            yield param
        for param in self.dla_up.parameters():
            yield param
        for param in self.fc.parameters():
            yield param
