
import torch as T
import torch.nn as NN
import torch.nn.functional as F
import torch.nn.init as INIT
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
import os
import tensorflow as TF
import numpy as np
import matplotlib.pyplot as PL
from PIL import Image
import matplotlib
import pickle
import matplotlib.pyplot as plt
import sys
import nltk

matplotlib.use('Agg')
matplotlib.rcParams.update({'font.size': 11})



def cuda(obj):
    if os.getenv('USE_CUDA', None):
        if isinstance(obj, tuple):
            return tuple(cuda(o) for o in obj)
        elif hasattr(obj, 'cuda'):
            return obj.cuda()
    return obj

def tovar(*arrs, **kwargs):
    tensors = [(T.from_numpy(a) if isinstance(a, np.ndarray) else a) for a in arrs]
    vars_ = [T.autograd.Variable(t, **kwargs) for t in tensors]
    if os.getenv('USE_CUDA', None):
        vars_ = [v.cuda() for v in vars_]
    return vars_[0] if len(vars_) == 1 else vars_


def tonumpy(*vars_):
    arrs = [(v.data.cpu().numpy() if isinstance(v, T.autograd.Variable) else
             v.cpu().numpy() if T.is_tensor(v) else v) for v in vars_]
    return arrs[0] if len(arrs) == 1 else arrs


def div_roundup(x, d):
    return (x + d - 1) / d
def roundup(x, d):
    return (x + d - 1) / d * d

def log_sigmoid(x):
    return -F.softplus(-x)
def log_one_minus_sigmoid(x):
    return -x - F.softplus(-x)

def binary_cross_entropy_with_logits_per_sample(input, target, weight=None):
    if not target.is_same_size(input):
        raise ValueError("Target size ({}) must be the same as input size ({})".format(target.size(), input.size()))

    max_val = (-input).clamp(min=0)
    loss = input - input * target + max_val + ((-max_val).exp() + (-input - max_val).exp()).log()

    if weight is not None:
        loss = loss * weight

    return loss.sum(1)


def advanced_index(t, dim, index):
    return t.transpose(dim, 0)[index].transpose(dim, 0)


def length_mask(size, length):
    return mask_3d(size, length, False)

def mask_3d(size, length, nan=True):
    length = tonumpy(length)
    batch_size = size[0]
    weight = T.zeros(*size)
    if nan:
        weight = weight / 0
    for i in range(batch_size):
        weight[i, :length[i],:] = 1.
    weight = tovar(weight)
    return weight

def mask_4d(size, length1, length2, nan=True):
    length1 = tonumpy(length1)
    length2 = tonumpy(length2)
    batch_size = size[0]
    weight = T.zeros(*size)
    if nan:
        weight = weight / 0
    for i in range(batch_size):
        for j in range(length1[i]):
            weight[i, :length1[i], :length2[i,j],:] = 1.
    weight = tovar(weight)
    return weight


def dynamic_rnn(rnn, seq, length, initial_state):
    length = length.clamp(min=1)
    length_sorted, length_sorted_idx = T.sort(length, 0, descending=True)
    _, length_inverse_idx = T.sort(length_sorted_idx)
    rnn_in = pack_padded_sequence(
            advanced_index(seq, 1, length_sorted_idx),
            tonumpy(length_sorted),
            )
    rnn_out, rnn_last_state = rnn(rnn_in, initial_state)
    rnn_out = pad_packed_sequence(rnn_out)[0]
    out = advanced_index(rnn_out, 1, length_inverse_idx)
    if isinstance(rnn_last_state, tuple):
        state = tuple(advanced_index(s, 1, length_inverse_idx) for s in rnn_last_state)
    else:
        state = advanced_index(s, 1, length_inverse_idx)

    return out, state



def check_grad(params):
    for p in params:
        if p.grad is None:
            continue
        g = p.grad.data
        anynan = (g != g).long().sum()
        anybig = (g.abs() > 1e+5).long().sum()
        if anynan or anybig:
            return False
    return True

def adjust_learning_rate(optimizer, lr):
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
        
def clip_grad(params, clip_norm):
    norm = np.sqrt(
            sum(p.grad.data.norm() ** 2
                for p in params if p.grad is not None
                )
            )
    if norm > clip_norm:
        for p in params:
            if p.grad is not None:
                p.grad /= norm / clip_norm
    return norm

def init_lstm(lstm):
    for name, param in lstm.named_parameters():
        if name.startswith('weight_ih'):
            INIT.xavier_uniform(param.data)
        elif name.startswith('weight_hh'):
            INIT.orthogonal(param.data)
        elif name.startswith('bias'):
            INIT.constant(param.data, 0)

def init_weights(module):
    for name, param in module.named_parameters():
        if name.find('weight') != -1:
            if len(param.size()) == 1:
                INIT.uniform(param.data, 1)
            else:
                INIT.xavier_uniform(param.data)
        elif name.find('bias') != -1:
            INIT.constant(param.data, 0)

class Residual(NN.Module):
    def __init__(self,size, relu = True):
        NN.Module.__init__(self)
        self.size = size
        self.linear = NN.Linear(size, size)
        if relu:
            self.relu = NN.LeakyReLU()
        else:
            self.relu = False

    def forward(self, x):
        if self.relu:
            return self.relu(self.linear(x) + x)
        else:
            return self.linear(x) + x

class ConvMask(NN.Module):
    def __init__(self):
        NN.Module.__init__(self)

    def forward(self, x):
        global convlengths
        mask = length_mask((x.size()[0], x.size()[2]),convlengths).unsqueeze(1)
        x = x * mask
        return x
def gaussian(ins, is_training, mean, stddev):
    if is_training:
        noise = tovar(ins.data.new(ins.size()).normal_(mean, stddev))
        return ins + noise
    return ins
class HierarchicalLogSoftmax(NN.Module):
    def __init__(self, input_size, n_classes, n_words):
        '''
        n_words must be dividable by n_classes
        '''
        NN.Module.__init__(self)
        assert n_words % n_classes == 0
        self.n_words_per_cls = n_words // n_classes
        self.n_classes = n_classes

        self.W_cls = NN.Parameter(T.zeros(input_size, n_classes))
        self.b_cls = NN.Parameter(T.zeros(n_classes))
        self.W_word_in_cls = NN.Parameter(T.zeros(n_classes, input_size, self.n_words_per_cls))
        self.b_word_in_cls = NN.Parameter(T.zeros(n_classes, self.n_words_per_cls))

        INIT.xavier_uniform(self.W_cls)
        INIT.xavier_uniform(self.W_word_in_cls)
        INIT.constant(self.b_cls, 0)
        INIT.constant(self.b_word_in_cls, 0)

        # self.mapping[x]
        perm = np.random.permutation(n_words)
        mapping_dict = dict(zip(range(n_words), perm))
        inv_mapping_dict = dict(zip(perm, range(n_words)))
        self.mapping = cuda(T.LongTensor(np.array([mapping_dict[i] for i in range(n_words)], dtype='int64')))
        self.inv_mapping = cuda(T.LongTensor(np.array([inv_mapping_dict[i] for i in range(n_words)], dtype='int64')))

    def forward(self, x, target=None):
        '''
        x: (batch_size, input_size)
        target: LongTensor (batch_size,) or None

        return:
        if target is None, returns
            prob: (batch_size, n_words)
        if target is a LongTensor, returns
            prob: (batch_size)
        '''
        batch_size = x.size()[0]
        cls_prob = F.log_softmax(x @ self.W_cls + self.b_cls)
        if target is None:
            word_in_cls_logit = x.unsqueeze(0) @ self.W_word_in_cls + self.b_word_in_cls.unsqueeze(1)
            word_in_cls_prob = F.log_softmax(word_in_cls_logit.view(self.n_classes * batch_size, -1))
            word_in_cls_prob = word_in_cls_prob.view(self.n_classes, batch_size, -1)
            word_prob = cls_prob.transpose(1, 0).unsqueeze(2) + word_in_cls_prob
            word_prob = word_prob.transpose(1, 0).contiguous().view(batch_size, -1)

            return word_prob[:, self.mapping]
        else:
            internal_target = tovar(self.mapping)[target]
            internal_target_cls = internal_target / self.n_words_per_cls
            internal_target_word_in_cls = internal_target % self.n_words_per_cls

            target_cls_prob = cls_prob.gather(1, internal_target_cls.unsqueeze(1))
            target_W = self.W_word_in_cls[internal_target_cls]
            target_b = self.b_word_in_cls[internal_target_cls]
            target_word_in_cls_prob = F.log_softmax((x.unsqueeze(1) @ target_W).squeeze(1) + target_b)

            word_prob = target_cls_prob + target_word_in_cls_prob
            word_prob = word_prob.gather(1, internal_target_word_in_cls.unsqueeze(1))

            return word_prob

style_map = {0: 'constant', 1: 'gradient'}
def add_scatterplot(writer, losses, scales, names, itr, log_dir, 
                    tag = 'scatterplot', style = 0):
    png_file = '%s/temp11.png' % log_dir
    PL.figure(figsize=(6,6))
    for loss_list, scale_list, name in zip(losses, scales, names):
        PL.scatter(scale_list, loss_list, label = name, alpha=.5)
    PL.xlabel('scales')
    PL.xscale('log')
    PL.ylabel('adv loss change')
    PL.title(style_map[style])
    PL.legend()
    PL.tight_layout()
    axes = PL.gca()
    y = np.array(losses)
    rnge = y.max() - y.min()
    axes.set_ylim([y.min() - rnge/100,y.max() + rnge/100])
    PL.savefig(png_file)
    PL.close()
    with open(png_file, 'rb') as f:
        imgbuf = f.read()
    img = Image.open(png_file)
    summary = TF.Summary.Image(
            height=img.height,
            width=img.width,
            colorspace=3,
            encoded_image_string=imgbuf
            )
    summary = TF.Summary.Value(tag='%s' % (tag), image=summary)
    writer.add_summary(TF.Summary(value=[summary]), itr)


def weighted_softmax(logits, weights=None):
    '''
    Inputs:
    logits: (batch_size, max_num_elements), FloatTensor
    weights: (batch_size, max_num_elements), FloatTensor
    Computes:
    p[i] = (w[i] * exp(l[i])) / sum(w[j] * exp(l[j]))
    '''
    wl = T.exp(logits - logits.max(1, keepdim=True)[0]) * (weights if weights is not None else 1)
    wl = wl / (wl.sum(1)+1e-8).unsqueeze(1)
    return wl

# Preprocess glove : Download wikipedia embeddings from :
# https://nlp.stanford.edu/projects/glove/
# file : glove.6B.zip


def preprocess_glove(dataroot, emb_size=50):
    with open("wordcount.pkl", 'rb') as f:
        wordcount = pickle.load(f)

    print("Preprocess glove ...")
    embsize_to_txt = {50: "glove.6B.50d.txt", 100: "glove.6B.100d.txt", 200: "glove.6B.200d.txt",
                      300: "glove.6B.300d.txt"}

    match = {}
    with open(dataroot + "/" + embsize_to_txt[emb_size], 'r') as f:
        i = 0

        for line in f:
            if i % 1000 == 0:
                print("line : %d match : %d" % (i, len(match)))
            i += 1

            line = line.split(" ")
            word = line[0]
            if word in wordcount.keys():
                vec = T.from_numpy(np.array([float(i) for i in line[1:]]))
                match[word] = vec
    with open(dataroot + "/glove-match-" + str(emb_size) + ".pkl", 'wb') as f:
        pickle.dump(match, f)
    print("File dumped ... ")

def init_glove(word_emb, vcb, ivcb, dataroot):
    """
    :param word_emb: randomly initialized word embedding.
    :param vcb: list(words) vocab.
    :param ivcb: word -> idx
    :return: floatTensor(Vocab size x word_emb_size)
    """

    embsize_to_txt = {50:"glove-match-50.pkl", 100:"glove-match-100.pkl"}
    emb = word_emb.weight.data
    emb_size = word_emb.weight.size(1)
    if emb_size not in [50, 100, 200, 300]:
        print('Glove embedding size should be in [50,100,200,300], but is set to %d' % emb_size)
        return emb

    match = 0

    with open(dataroot + "/" + embsize_to_txt[emb_size], 'rb') as f:
        glove_wd_to_emb = pickle.load(f)

    for wd in vcb:
        if wd in glove_wd_to_emb.keys():
            emb[ivcb[wd]] = glove_wd_to_emb[wd]
            match += 1

    print("Match words in glove : " + str(match))
    return emb

def round_robin_dataloader(dataloaders):
    dataloader_iters = [iter(dataloader) for dataloader in dataloaders]
    i = 0

    while True:
        try:
            item = next(dataloader_iters[i])
        except StopIteration:
            dataloader_iters[i] = iter(dataloaders[i])
            item = next(dataloader_iters[i])
        i = (i + 1) % len(dataloaders)
        yield item

def batch_to_string_sentence(words_padded, lengths, dataset):
    batch_size = lengths.size(0)
    max_turn = lengths.size(1)

    out = []

    for batch_elem in range(batch_size):
        batch_string = []
        for turn in range(max_turn):
            if lengths[batch_elem,turn] == 0:
                break
            batch_string += [words_padded[batch_elem,turn,:lengths[batch_elem,turn]].cpu().numpy()]

        list_list_string = dataset.translate_item(None, None, batch_string)[2]
        sentences = []

        for list_string in list_list_string:
            sentence = []
            for i in range(0, len(list_string)):
                sentence += [list_string[i]]

            sentences += [sentence]

        out += [sentences]

    return out

def join_sentence(str_array):
    out = ""
    for i in range(1, len(str_array)):
        out += str_array[i]
        if i % 6 == 5:
            out += "\n"
        else:
            out += " "
    return out

def plot_attention(attn_weight, attended_over=None, mask=None, print_path="output/attention_elem_%d.png"):
    """
    :param attn_weight: batch_size x elem_num x elem_num
    :param attended_over: batch_size x elem_num
    :param print_path: path that can work with % (batch_elem)
    """
    # print(mask.size())
    # print(mask[0].squeeze().sum(2))

    #print(attn_weight[0].squeeze().sum(2), attn_weight[0].squeeze().sum(2)[0])
    elem_num = attn_weight.size(1)
    batch_size = attn_weight.size(0)

    joint_sentences = [[join_sentence(attended_over[batch_elem][sentence_elem][:-1]) for sentence_elem in range(elem_num)] for batch_elem in range(batch_size)]

    elem = 0

    for batch_elem in range(batch_size):
        for sentence_elem in range(elem_num - 1):
            elem += 1
            str_convers = attended_over[batch_elem][sentence_elem]
            convers_size = len(str_convers)
            #print(attn_weight[batch_elem,:,:,:convers_size].sum(0), attn_weight[batch_elem,:,:,:convers_size].sum(1), attn_weight[batch_elem,:,:,:convers_size].sum(2))
            matrix = attn_weight[batch_elem, :, sentence_elem, :convers_size].squeeze()[sentence_elem + 1:].cpu().data.numpy()
            fig, ax = plt.subplots()
            # plt.gcf().subplots_adjust(bottom=0.15)
            heatmap = ax.pcolor(matrix, cmap=plt.cm.Blues, alpha=0.8, vmin=matrix.min(), vmax=matrix.max())

            # Format
            fig = plt.gcf()

            fig.set_size_inches(8, 11)

            # turn off the frame
            ax.set_frame_on(False)

            # put the major ticks at the middle of each cell
            ax.set_yticks(np.arange(convers_size) + 0.5, minor=False)
            ax.set_xticks(np.arange(convers_size) + 0.5, minor=False)
            plt.tick_params(axis='both', which='major', labelsize=8)
            # want a more natural, table-like display
            ax.invert_yaxis()
            ax.xaxis.tick_top()

            # note I could have used nba_sort.columns but made "labels" instead
            ax.set_xticklabels(str_convers, minor=False)
            ax.set_yticklabels(joint_sentences[batch_elem][sentence_elem + 1:], minor=False)
            # rotate the
            plt.xticks(rotation=90)
            plt.yticks(rotation=45)
            plt.tight_layout()

            ax.grid(False)

            # Turn off all the ticks
            ax = plt.gca()

            for t in ax.xaxis.get_major_ticks():
                t.tick1On = False
                t.tick2On = False
            for t in ax.yaxis.get_major_ticks():
                t.tick1On = False
                t.tick2On = False

            plt.savefig(print_path % (elem + 1))
            plt.close()
