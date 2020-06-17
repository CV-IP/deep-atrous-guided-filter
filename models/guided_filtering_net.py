import torch
import torch.nn as nn
from torch.nn import functional as F
from torch.nn import init
from utils.model_serialization import load_state_dict
from utils.ops import unpixel_shuffle

from models.guided_filter import FastGuidedFilter, ConvGuidedFilter
from models.resunet_pixel_shuffle import ResUnet
from models.DenoisingModels import ntire_rdb_gd_rir_ver2

from sacred import Experiment
from config import initialise
from utils.tupperware import tupperware

ex = Experiment("DGF")
ex = initialise(ex)


def weights_init_identity(m):
    classname = m.__class__.__name__
    if classname.find("Conv") != -1:
        n_out, n_in, h, w = m.weight.data.size()
        # Last Layer
        if n_out < n_in:
            init.xavier_uniform_(m.weight.data)
            return

        # Except Last Layer
        m.weight.data.zero_()
        ch, cw = h // 2, w // 2
        for i in range(n_in):
            m.weight.data[i, i, ch, cw] = 1.0

    elif classname.find("BatchNorm2d") != -1:
        init.constant_(m.weight.data, 1.0)
        init.constant_(m.bias.data, 0.0)


class AdaptiveNorm(nn.Module):
    def __init__(self, n):
        super(AdaptiveNorm, self).__init__()

        self.w_0 = nn.Parameter(torch.Tensor([1.0]))
        self.w_1 = nn.Parameter(torch.Tensor([0.0]))

        self.bn = nn.BatchNorm2d(n, momentum=0.999, eps=0.001)

    def forward(self, x):
        return self.w_0 * x + self.w_1 * self.bn(x)


def build_lr_net(norm=AdaptiveNorm, layer=5):
    layers = [
        nn.Conv2d(3, 24, kernel_size=3, stride=1, padding=1, dilation=1, bias=False),
        norm(24),
        nn.LeakyReLU(0.2, inplace=True),
    ]

    for l in range(1, layer):
        layers += [
            nn.Conv2d(
                24,
                24,
                kernel_size=3,
                stride=1,
                padding=2 ** l,
                dilation=2 ** l,
                bias=False,
            ),
            norm(24),
            nn.LeakyReLU(0.2, inplace=True),
        ]

    layers += [
        nn.Conv2d(24, 24, kernel_size=3, stride=1, padding=1, dilation=1, bias=False),
        norm(24),
        nn.LeakyReLU(0.2, inplace=True),
        nn.Conv2d(24, 3, kernel_size=1, stride=1, padding=0, dilation=1),
    ]

    net = nn.Sequential(*layers)

    net.apply(weights_init_identity)

    return net


def build_lr_net_pixelshuffle(args, norm=AdaptiveNorm, layer=5):
    layers = [
        nn.Conv2d(
            3 * args.pixelshuffle_ratio ** 2,
            48,
            kernel_size=3,
            stride=1,
            padding=1,
            dilation=1,
            bias=False,
        ),
        norm(48),
        nn.LeakyReLU(0.2, inplace=True),
    ]

    for l in range(1, layer):
        layers += [
            nn.Conv2d(
                48,
                48,
                kernel_size=3,
                stride=1,
                padding=2 ** l,
                dilation=2 ** l,
                bias=False,
            ),
            norm(48),
            nn.LeakyReLU(0.2, inplace=True),
        ]

    layers += [
        nn.Conv2d(48, 48, kernel_size=3, stride=1, padding=1, dilation=1, bias=False),
        norm(48),
        nn.LeakyReLU(0.2, inplace=True),
        nn.Conv2d(
            48,
            3 * args.pixelshuffle_ratio ** 2,
            kernel_size=1,
            stride=1,
            padding=0,
            dilation=1,
        ),
    ]

    net = nn.Sequential(*layers)

    net.apply(weights_init_identity)

    return net


class DeepGuidedFilter(nn.Module):
    def __init__(self, radius=1, eps=1e-8):
        super(DeepGuidedFilter, self).__init__()
        self.lr = build_lr_net()
        self.gf = FastGuidedFilter(radius, eps)

    def forward(self, x_lr, x_hr):
        return self.gf(x_lr, self.lr(x_lr), x_hr).clamp(0, 1)

    def init_lr(self, path):
        checkpoint = torch.load(path, map_location=torch.device("cpu"))
        load_state_dict(self.lr, checkpoint["state_dict"])

        # self.lr.load_state_dict(torch.load(path))


class DeepGuidedFilterAdvanced(DeepGuidedFilter):
    def __init__(self, radius=1, eps=1e-4):
        super(DeepGuidedFilterAdvanced, self).__init__(radius, eps)

        self.guided_map = nn.Sequential(
            nn.Conv2d(3, 15, 1, bias=False),
            AdaptiveNorm(15),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Conv2d(15, 3, 1),
        )
        self.guided_map.apply(weights_init_identity)

    def forward(self, x_lr, x_hr):
        return self.gf(self.guided_map(x_lr), self.lr(x_lr), self.guided_map(x_hr))


class DeepGuidedFilterConvGF(nn.Module):
    def __init__(self, radius=1, layer=5):
        super(DeepGuidedFilterConvGF, self).__init__()
        self.lr = build_lr_net(layer=layer)
        self.gf = ConvGuidedFilter(radius, norm=AdaptiveNorm)

    def forward(self, x_lr, x_hr):
        return F.tanh(self.gf(x_lr, self.lr(x_lr), x_hr))

    def init_lr(self, path):
        self.lr.load_state_dict(torch.load(path))


class DeepGuidedFilterGuidedMapConvGF(DeepGuidedFilterConvGF):
    def __init__(self, radius=1, dilation=0, c=16, layer=5):
        super(DeepGuidedFilterGuidedMapConvGF, self).__init__(radius, layer)

        self.guided_map = nn.Sequential(
            nn.Conv2d(3, c, 1, bias=False)
            if dilation == 0
            else nn.Conv2d(3, c, 3, padding=dilation, dilation=dilation, bias=False),
            AdaptiveNorm(c),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Conv2d(c, 3, 1),
        )

        self.downsample = nn.Upsample(
            size=(256, 512), mode="bilinear", align_corners=True
        )

    def forward(self, x_hr):
        x_lr = self.downsample(x_hr)
        return F.tanh(
            self.gf(self.guided_map(x_lr), self.lr(x_lr), self.guided_map(x_hr))
        )


class DeepGuidedFilterGuidedMapConvGFPixelShuffle(DeepGuidedFilterConvGF):
    def __init__(self, args, radius=1, dilation=0, c=16, layer=5):
        super(DeepGuidedFilterGuidedMapConvGFPixelShuffle, self).__init__(radius, layer)

        self.guided_map = nn.Sequential(
            nn.Conv2d(3, c, 1, bias=False)
            if dilation == 0
            else nn.Conv2d(3, c, 3, padding=dilation, dilation=dilation, bias=False),
            AdaptiveNorm(c),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Conv2d(c, 3, 1),
        )
        self.lr = build_lr_net_pixelshuffle(args, layer=args.CAN_layers)

        self.downsample = nn.Upsample(
            scale_factor=0.5, mode="bilinear", align_corners=True
        )

    def forward(self, x_hr):
        x_lr = self.downsample(x_hr)
        x_lr_unpixelshuffled = unpixel_shuffle(x_lr, 2)
        y_lr = F.pixel_shuffle(self.lr(x_lr_unpixelshuffled), 2)

        return F.tanh(self.gf(self.guided_map(x_lr), y_lr, self.guided_map(x_hr)))


class DeepGuidedFilterGuidedMapConvGFGDRN(DeepGuidedFilterConvGF):
    def __init__(self, args, radius=1, dilation=0, c=16, layer=5):
        super(DeepGuidedFilterGuidedMapConvGFGDRN, self).__init__(radius, layer)

        self.guided_map = nn.Sequential(
            nn.Conv2d(3, c, 1, bias=False)
            if dilation == 0
            else nn.Conv2d(3, c, 3, padding=dilation, dilation=dilation, bias=False),
            AdaptiveNorm(c),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Conv2d(c, 3, 1),
        )
        self.lr = ntire_rdb_gd_rir_ver2(input_channel=12, numofrdb=12)

        self.downsample = nn.Upsample(
            size=(256, 512), mode="bilinear", align_corners=True
        )

    def forward(self, x_hr):
        x_lr = self.downsample(x_hr)

        x_lr_unpixelshuffled = unpixel_shuffle(x_lr, 2)
        o_lr = F.pixel_shuffle(self.lr(x_lr_unpixelshuffled), 2)
        return F.tanh(self.gf(self.guided_map(x_lr), o_lr, self.guided_map(x_hr)))


@ex.automain
def main(_run):
    from torchsummary import summary

    args = tupperware(_run.config)
    model = DeepGuidedFilterGuidedMapConvGFPixelShuffle(args)

    summary(model, (3, 1024, 2048))
