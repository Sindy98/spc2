import torch
import torch.nn as nn
# import torch.nn.functional as F
from torch.autograd import Variable
from utils.util import weights_init

class convLSTMCell(nn.Module):
    def __init__(self, in_channels, feature_channels, kernel_size, stride = 1, padding = 0, dilation = 1, groups = 1, bias = True):
        super(convLSTMCell, self).__init__()
        self.feature_channels = feature_channels
        self.conv = nn.Conv2d(in_channels + feature_channels, 4 * feature_channels, kernel_size, stride, padding, dilation, groups, bias)
        self.apply(weights_init)
    
    def forward(self, x, hidden_states):
        hx, cx = hidden_states
        combined = torch.cat([x, hx], dim=1)
        A = self.conv(combined)
        (ai, af, ao, ag) = torch.split(A, self.feature_channels, dim=1)#it should return 4 tensors
        i = torch.sigmoid(ai)
        f = torch.sigmoid(af)
        o = torch.sigmoid(ao)
        g = torch.tanh(ag)

        next_c = f * cx + i * g
        next_h = o * torch.tanh(next_c)
        
        return next_h, next_c

class convLSTM(nn.Module):
    def __init__(self):
        super(convLSTM, self).__init__()
        self.lstm0 = convLSTMCell(66, 64, kernel_size=5, padding=2)
        self.lstm1 = convLSTMCell(66, 64, kernel_size=3, padding=1)
        self.lstm2 = convLSTMCell(130, 128, kernel_size=3, padding=1)
        self.lstm3 = convLSTMCell(258, 256, kernel_size=1, padding=0)

    def forward(self, x):
        batch_size = x[0][0].shape[0]

        # The last two dimensions should be: frame_height / 4, frame_width / 4
        hx0 = Variable(torch.zeros(batch_size, 64, 64, 128))
        cx0 = Variable(torch.zeros(batch_size, 64, 64, 128))
        # The last two dimensions should be: frame_height / 8, frame_width / 8
        hx1 = Variable(torch.zeros(batch_size, 64, 32, 64))
        cx1 = Variable(torch.zeros(batch_size, 64, 32, 64))
        # The last two dimensions should be: frame_height / 16, frame_width / 16
        hx2 = Variable(torch.zeros(batch_size, 128, 16, 32))
        cx2 = Variable(torch.zeros(batch_size, 128, 16, 32))
        # The last two dimensions should be: frame_height / 32, frame_width / 32
        hx3 = Variable(torch.zeros(batch_size, 256, 8, 16))
        cx3 = Variable(torch.zeros(batch_size, 256, 8, 16))
        
        if torch.cuda.is_available():
            hx0 = hx0.cuda()
            cx0 = cx0.cuda()
            hx1 = hx1.cuda()
            cx1 = cx1.cuda()
            hx2 = hx2.cuda()
            cx2 = cx2.cuda()
            hx3 = hx3.cuda()
            cx3 = cx3.cuda()

        for step in range(len(x)):
            hx0, cx0 = self.lstm0(x[step][0], (hx0, cx0))
            hx1, cx1 = self.lstm1(x[step][1], (hx1, cx1))
            hx2, cx2 = self.lstm2(x[step][2], (hx2, cx2))
            hx3, cx3 = self.lstm3(x[step][3], (hx3, cx3))

        return [hx0, hx1, hx2, hx3]

if __name__ == '__main__':
    net = convLSTM()
    x1 = [Variable(torch.rand(2, 66, 64, 64)), Variable(torch.rand(2, 66, 32, 32)), Variable(torch.rand(2, 130, 16, 16)), Variable(torch.rand(2, 258, 8, 8))]
    x2 = [Variable(torch.rand(2, 66, 64, 64)), Variable(torch.rand(2, 66, 32, 32)), Variable(torch.rand(2, 130, 16, 16)), Variable(torch.rand(2, 258, 8, 8))]
    x3 = [Variable(torch.rand(2, 66, 64, 64)), Variable(torch.rand(2, 66, 32, 32)), Variable(torch.rand(2, 130, 16, 16)), Variable(torch.rand(2, 258, 8, 8))]
    y = net([x1, x2, x3])
  
    for t in y:
        print(t.size())
