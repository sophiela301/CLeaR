import torch
import torch.nn as nn
from util.sampler import next_batch_pairwise
from util.loss import bpr_loss, l2_reg_loss, InfoNCE, batch_softmax_loss
import torch.nn.functional as F
import time
from util.algorithm import find_k_largest
from time import strftime, localtime, time
from os.path import abspath
import sys
from util.metrics import ranking_evaluation
from util.FileIO import FileIO
from util.logger import Log
import scipy.sparse as sp
import numpy as np


class SSL4Rec():
    def __init__(self, args, data):
        print("Recommender: SSL4Rec")
        self.data = data
        self.args = args
        self.bestPerformance = []
        self.recOutput = []
        top = self.args.topK.split(',')
        self.topN = [int(num) for num in top]
        self.max_N = max(self.topN)

        # Hyperparameter
        # SimGCL=-n_layer 2 -lambda 0.5 -eps 0.1
        self.n_layers = 2
        self.cl_rate = 1
        self.tau = 0.2
        self.drop_rate = 0.2
        self.model = DNN_Encoder(self.data, self.args.emb_size, self.drop_rate, self.tau, self.n_layers)

    def train(self, requires_adjgrad=False, requires_embgrad=False, gradIterationNum=10, Epoch=0, optimizer=None, evalNum=5):
        self.bestPerformance=[]
        model = self.model.cuda()
        if optimizer is None: optimizer = torch.optim.Adam(model.parameters(), lr=self.args.lRate)
        if requires_embgrad:
            model.requires_grad = True
            self.usergrad = torch.zeros((self.data.user_num, self.args.emb_size)).cuda()
            self.itemgrad = torch.zeros((self.data.item_num, self.args.emb_size)).cuda()
        elif requires_adjgrad:
            self.model.sparse_norm_adj.requires_grad = True
            self.Matgrad = torch.zeros(
                (self.data.user_num + self.data.item_num, self.data.user_num + self.data.item_num)).cuda()
        maxEpoch = self.args.maxEpoch
        if Epoch: maxEpoch = Epoch
        for epoch in range(maxEpoch):
            for n, batch in enumerate(next_batch_pairwise(self.data, self.args.batch_size)):
                user_idx, pos_idx, neg_idx = batch
                model.train()
                rec_user_emb, rec_item_emb = model()
                user_emb, pos_item_emb, neg_item_emb = rec_user_emb[user_idx], rec_item_emb[pos_idx], rec_item_emb[
                    neg_idx]
                rec_loss = bpr_loss(user_emb, pos_item_emb, neg_item_emb)
                cl_loss = self.cl_rate * model.cal_cl_loss(user_idx, pos_idx)
                batch_loss = rec_loss + l2_reg_loss(self.args.reg, user_emb, pos_item_emb) + cl_loss
                # Backward and optimize
                optimizer.zero_grad()
                batch_loss.backward()

                if requires_adjgrad and maxEpoch - epoch < gradIterationNum:
                    self.Matgrad += self.model.sparse_norm_adj.grad
                elif requires_embgrad and maxEpoch - epoch < gradIterationNum:
                    self.usergrad += self.model.embedding_dict["user_emb"].grad
                    self.itemgrad += self.model.embedding_dict["item_emb"].grad

                optimizer.step()
                if n % 100 == 0:
                    print('training:', epoch + 1, 'batch', n, 'rec_loss:', rec_loss.item(), 'cl_loss', cl_loss.item())
            model.eval()
            with torch.no_grad():
                self.user_emb, self.item_emb = self.model()
            if epoch % evalNum == 0:
                self.evaluate(epoch)
        self.user_emb, self.item_emb = self.best_user_emb, self.best_item_emb
        if requires_adjgrad and requires_embgrad:
            return (self.Matgrad + self.Matgrad.T)[:self.data.user_num, self.data.user_num:], \
                   self.user_emb, self.item_emb, self.usergrad, self.itemgrad
        elif requires_adjgrad:
            return (self.Matgrad + self.Matgrad.T)[:self.data.user_num, self.data.user_num:]
        elif requires_embgrad:
            return self.user_emb, self.item_emb, self.usergrad, self.itemgrad

    def save(self):
        with torch.no_grad():
            self.best_user_emb, self.best_item_emb = self.model.forward()

    def predict(self, u):
        with torch.no_grad():
            u = self.data.get_user_id(u)
            score = torch.matmul(self.user_emb[u], self.item_emb.transpose(0, 1))
            return score.cpu().numpy()

    def evaluate(self, epoch):
        print('Evaluating the model...')
        rec_list, _ = self.test()
        measure = ranking_evaluation(self.data.test_set, rec_list, [self.max_N])
        if len(self.bestPerformance) > 0:
            count = 0
            performance = {}
            for m in measure[1:]:
                k, v = m.strip().split(':')
                performance[k] = float(v)
            for k in self.bestPerformance[1]:
                if self.bestPerformance[1][k] > performance[k]:
                    count += 1
                else:
                    count -= 1
            if count < 0:
                self.bestPerformance[1] = performance
                self.bestPerformance[0] = epoch + 1
                self.save()
        else:
            self.bestPerformance.append(epoch + 1)
            performance = {}
            for m in measure[1:]:
                k, v = m.strip().split(':')
                performance[k] = float(v)
            self.bestPerformance.append(performance)
            self.save()
        print('-' * 120)
        print('Real-Time Ranking Performance ' + ' (Top-' + str(self.max_N) + ' Item Recommendation)')
        measure = [m.strip() for m in measure[1:]]
        print('*Current Performance*')
        print('Epoch:', str(epoch + 1) + ',', '  |  '.join(measure))
        bp = ''
        # for k in self.bestPerformance[1]:
        #     bp+=k+':'+str(self.bestPerformance[1][k])+' | '
        bp += 'Hit Ratio' + ':' + str(self.bestPerformance[1]['Hit Ratio']) + '  |  '
        bp += 'Precision' + ':' + str(self.bestPerformance[1]['Precision']) + '  |  '
        bp += 'Recall' + ':' + str(self.bestPerformance[1]['Recall']) + '  |  '
        # bp += 'F1' + ':' + str(self.bestPerformance[1]['F1']) + ' | '
        bp += 'NDCG' + ':' + str(self.bestPerformance[1]['NDCG'])
        print('*Best Performance* ')
        print('Epoch:', str(self.bestPerformance[0]) + ',', bp)
        print('-' * 120)
        return measure

    def test(self):
        def process_bar(num, total):
            rate = float(num) / total
            ratenum = int(50 * rate)
            r = '\rProgress: [{}{}]{}%'.format('+' * ratenum, ' ' * (50 - ratenum), ratenum*2)
            sys.stdout.write(r)
            sys.stdout.flush()

        # predict
        rec_list = {}
        user_count = len(self.data.test_set)
        for i, user in enumerate(self.data.test_set):
            candidates = self.predict(user)
            # predictedItems = denormalize(predictedItems, self.data.rScale[-1], self.data.rScale[0])
            rated_list, li = self.data.user_rated(user)
            for item in rated_list:
                candidates[self.data.item[item]] = -10e8
            ids, scores = find_k_largest(self.max_N, candidates)
            item_names = [self.data.id2item[iid] for iid in ids]
            rec_list[user] = list(zip(item_names, scores))
            if i % 1000 == 0:
                process_bar(i, user_count)
        process_bar(user_count, user_count)
        print('')
        return rec_list,ranking_evaluation(self.data.test_set, rec_list, self.topN)

class DNN_Encoder(nn.Module):
    def __init__(self, data, emb_size, drop_rate, temperature, n_layers):
        super(DNN_Encoder, self).__init__()
        self.data = data
        self.emb_size = emb_size
        self.tau = temperature
        self.user_tower = nn.Sequential(
            nn.Linear(self.emb_size, 1024),
            nn.ReLU(True),
            nn.Linear(1024, 128),
            nn.Tanh()
        )
        self.item_tower = nn.Sequential(
            nn.Linear(self.emb_size, 1024),
            nn.ReLU(True),
            nn.Linear(1024, 128),
            nn.Tanh()
        )
        self.dropout = nn.Dropout(drop_rate)
        self.embedding_dict = self._init_model()

        self.layers = n_layers
        self.norm_adj = data.norm_adj
        self.embedding_dict = self._init_model()
        self.sparse_norm_adj = TorchGraphInterface.convert_sparse_mat_to_tensor(self.norm_adj).cuda()
        
        # initializer = nn.init.xavier_uniform_

        # self.initial_user_emb = nn.Parameter(initializer(torch.empty(self.data.user_num, self.emb_size)))
        # self.initial_item_emb = nn.Parameter(initializer(torch.empty(self.data.item_num, self.emb_size)))

    def _init_uiAdj(self, ui_adj):
        self.sparse_norm_adj = sp.diags(np.array((1 / np.sqrt(ui_adj.sum(1)))).flatten()) @ ui_adj @ sp.diags(
            np.array((1 / np.sqrt(ui_adj.sum(0)))).flatten())
        self.sparse_norm_adj = TorchGraphInterface.convert_sparse_mat_to_tensor(self.sparse_norm_adj).cuda()

    def _init_model(self):
        initializer = nn.init.xavier_uniform_
        embedding_dict = nn.ParameterDict({
            'user_emb': nn.Parameter(initializer(torch.empty(self.data.user_num, self.emb_size))),
            'item_emb': nn.Parameter(initializer(torch.empty(self.data.item_num, self.emb_size))),
        })
        return embedding_dict

    def forward(self):
        # self.initial_user_emb = self.embedding_dict['user_emb']
        # self.initial_item_emb = self.embedding_dict['item_emb']
        ego_embeddings = torch.cat([self.embedding_dict['user_emb'], self.embedding_dict['item_emb']], 0)
        all_embeddings = [ego_embeddings]
        for k in range(self.layers):
            ego_embeddings = torch.sparse.mm(self.sparse_norm_adj, ego_embeddings)
            all_embeddings += [ego_embeddings]
        all_embeddings = torch.stack(all_embeddings, dim=1)
        all_embeddings = torch.mean(all_embeddings, dim=1)
        self.initial_user_emb = all_embeddings[:self.data.user_num]
        self.initial_item_emb = all_embeddings[self.data.user_num:]
        q_emb = self.initial_user_emb
        i_emb = self.initial_item_emb
        # q_emb = self.user_tower(q_emb)
        # i_emb = self.item_tower(i_emb)

        return q_emb, i_emb

    def item_encoding(self, x1, x2):
        u_emb = self.initial_user_emb[x1]
        u1_emb = self.dropout(u_emb)
        u2_emb = self.dropout(u_emb)

        i_emb = self.initial_item_emb[x2]
        i1_emb = self.dropout(i_emb)
        i2_emb = self.dropout(i_emb)

        return u1_emb, u2_emb, i1_emb, i2_emb

    def cal_cl_loss(self, uidx, iidx):
        user_view1, user_view_2, item_view1, item_view_2 = self.item_encoding(uidx, iidx)

        cl_loss = InfoNCE(item_view1, item_view_2, self.tau) + InfoNCE(user_view1, user_view_2, self.tau)
        return cl_loss


class SimGCL_Encoder(nn.Module):
    def __init__(self, data, emb_size, eps, n_layers):
        super(SimGCL_Encoder, self).__init__()
        self.data = data
        self.eps = eps
        self.emb_size = emb_size
        self.n_layers = n_layers
        self.norm_adj = data.norm_adj
        self.embedding_dict = self._init_model()
        self.sparse_norm_adj = TorchGraphInterface.convert_sparse_mat_to_tensor(self.norm_adj).cuda()

    def _init_uiAdj(self, ui_adj):
        self.sparse_norm_adj = sp.diags(np.array((1 / np.sqrt(ui_adj.sum(1)))).flatten()) @ ui_adj @ sp.diags(
            np.array((1 / np.sqrt(ui_adj.sum(0)))).flatten())
        self.sparse_norm_adj = TorchGraphInterface.convert_sparse_mat_to_tensor(self.sparse_norm_adj).cuda()

    def _init_model(self):
        initializer = nn.init.xavier_uniform_
        embedding_dict = nn.ParameterDict({
            'user_emb': nn.Parameter(initializer(torch.empty(self.data.user_num, self.emb_size))),
            'item_emb': nn.Parameter(initializer(torch.empty(self.data.item_num, self.emb_size))),
        })
        return embedding_dict

    def attack_emb(self, users_emb_grad, items_emb_grad):
        with torch.no_grad():
            self.embedding_dict['user_emb'] += users_emb_grad
            self.embedding_dict['item_emb'] += items_emb_grad

    def forward(self, perturbed=False):
        ego_embeddings = torch.cat([self.embedding_dict['user_emb'], self.embedding_dict['item_emb']], 0)
        all_embeddings = []
        for k in range(self.n_layers):
            ego_embeddings = torch.sparse.mm(self.sparse_norm_adj, ego_embeddings)
            if perturbed:
                random_noise = torch.rand_like(ego_embeddings).cuda()
                ego_embeddings += torch.sign(ego_embeddings) * F.normalize(random_noise, dim=-1) * self.eps
            all_embeddings.append(ego_embeddings)
        all_embeddings = torch.stack(all_embeddings, dim=1)
        all_embeddings = torch.mean(all_embeddings, dim=1)
        user_all_embeddings, item_all_embeddings = torch.split(all_embeddings, [self.data.user_num, self.data.item_num])
        return user_all_embeddings, item_all_embeddings

    def cal_cl_loss(self, idx):
        u_idx = torch.unique(torch.Tensor(idx[0]).type(torch.long)).cuda()
        i_idx = torch.unique(torch.Tensor(idx[1]).type(torch.long)).cuda()
        user_view_1, item_view_1 = self.forward(perturbed=True)
        user_view_2, item_view_2 = self.forward(perturbed=True)
        user_cl_loss = InfoNCE(user_view_1[u_idx], user_view_2[u_idx], 0.2)
        item_cl_loss = InfoNCE(item_view_1[i_idx], item_view_2[i_idx], 0.2)
        return user_cl_loss + item_cl_loss


class TorchGraphInterface(object):
    def __init__(self):
        pass

    @staticmethod
    def convert_sparse_mat_to_tensor(X):
        coo = X.tocoo()
        i = torch.LongTensor([coo.row, coo.col])
        v = torch.from_numpy(coo.data).float()
        return torch.sparse.FloatTensor(i, v, coo.shape)
