import torch, numpy as np
from typing import Dict, List

class DiffusionScheduler(torch.nn.Module):
    def __init__(self, argv:Dict):
        super().__init__()
        self.num_steps = argv['n_diff_steps'] + 1
        self.mode = argv['schedule_type']

        if self.mode == 'linear':
            beta_1 = argv['beta_1']
            beta_T = argv['beta_T']
            betas = torch.linspace(beta_1, beta_T, steps=self.num_steps)
        elif self.mode == 'cosine':
            def betas_fn(s):
                T = self.num_steps

                def f(t, T):
                    return (np.cos((t / T + s) / (1 + s) * np.pi / 2)) ** 2

                alphas = []
                f0 = f(0, T)
                for t in range(T + 1):
                    alphas.append(f(t, T) / f0)

                betas = []
                for t in range(1, T + 1):
                    betas.append(min(1 - alphas[t] / alphas[t - 1], 0.999))
                return betas

            if 'S' not in argv.keys():
                S = 0.008
            else:
                S = argv['S']
            betas = betas_fn(s=S)

            betas = torch.FloatTensor(betas)

        self.betas = torch.cat([torch.zeros([1]), betas], dim=0)     # Padding
        self.alphas = 1 - self.betas

        log_alphas = torch.log(self.alphas)
        for i in range(1, log_alphas.size(0)):  # 1 to T
            log_alphas[i] += log_alphas[i - 1]
        self.alpha_bars = log_alphas.exp()

        self.gamma0 = torch.zeros_like(self.betas)
        self.gamma1 = torch.zeros_like(self.betas)
        self.gamma2 = torch.zeros_like(self.betas)
        for t in range(2, self.num_steps + 1):  # 2 to T
            self.gamma0[t] = self.betas[t] * torch.sqrt(self.alpha_bars[t - 1]) / (1. - self.alpha_bars[t])
            self.gamma1[t] = (1. - self.alpha_bars[t - 1]) * torch.sqrt(self.alphas[t]) / (1. - self.alpha_bars[t])
            self.gamma2[t] = (1. - self.alpha_bars[t - 1]) * self.betas[t] / (1. - self.alpha_bars[t])

    def uniform_sample_t(self, batch_size:int) -> List:
        ts:np.ndarray = np.random.choice(np.arange(1, self.num_steps+1), batch_size)
        return ts.tolist()