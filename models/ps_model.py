""" transformer based on reviews
    Q+r_{u1}+r_{u2} <> r_1, r_2 (of a target i)
"""
"""
review_encoder
query_encoder
transformer
"""
import os
import torch
import torch.nn as nn
from models.PV import ParagraphVector
from models.PVC import ParagraphVectorCorruption
from models.text_encoder import AVGEncoder, FSEncoder
from models.transformer import TransformerEncoder
from models.optimizers import Optimizer
from others.logging import logger
from others.util import pad, load_pretrain_embeddings

def build_optim(args, model, checkpoint):
    """ Build optimizer """
    saved_optimizer_state_dict = None

    if args.train_from != '' and checkpoint is not None:
        optim = checkpoint['optim']
        saved_optimizer_state_dict = optim.optimizer.state_dict()
    else:
        optim = Optimizer(
            args.optim, args.lr, args.max_grad_norm,
            beta1=args.beta1, beta2=args.beta2,
            decay_method=args.decay_method,
            warmup_steps=args.warmup_steps)
        #self.start_decay_steps take effect when decay_method is not noam

    optim.set_parameters(list(model.named_parameters()))

    if args.train_from != '' and checkpoint is not None:
        optim.optimizer.load_state_dict(saved_optimizer_state_dict)
        if args.device == "cuda":
            for state in optim.optimizer.state.values():
                for k, v in state.items():
                    if torch.is_tensor(v):
                        state[k] = v.cuda()

        if (optim.method == 'adam') and (len(optim.optimizer.state) < 1):
            raise RuntimeError(
                "Error: loaded Adam optimizer from existing model" +
                " but optimizer state is empty")

    return optim

class ProductRanker(nn.Module):
    def __init__(self, args, device, vocab_size, review_count, padded_review_words, word_dists=None):
        super(ProductRanker, self).__init__()
        self.args = args
        self.device = device
        self.train_review_only = args.train_review_only
        self.embedding_size = args.embedding_size
        self.word_dists = None
        if word_dists is not None:
            self.word_dists = torch.tensor(word_dists) #.to(device)
        self.review_words = torch.tensor(padded_review_words)
        self.word_pad_idx = vocab_size-1
        self.seg_pad_idx = 3
        self.review_pad_idx = review_count-1
        self.emb_dropout = args.dropout
        self.review_encoder_name = args.review_encoder_name
        self.fix_emb = args.fix_emb
        self.pretrain_emb_dir = args.pretrain_emb_dir
        self.dropout_layer = nn.Dropout(p=args.dropout)
        if self.fix_emb and args.review_encoder_name == "pvc":
            #if review embeddings are fixed, just load the aggregated embeddings which include all the words in the review
            #otherwise the reviews are cut off at review_word_limit
            self.review_encoder_name = "pv"

        if args.pretrain_emb_dir != "":
            word_emb_fname = "word_emb.txt.gz" if self.review_encoder_name == "pv" else "context_emb.txt.gz"
            pretrain_word_emb_path = os.path.join(args.pretrain_emb_dir, word_emb_fname)
            pretrained_weights = torch.FloatTensor(load_pretrain_embeddings(pretrain_word_emb_path))
            self.word_embeddings = nn.Embedding.from_pretrained(pretrained_weights)
        else:
            self.word_embeddings = nn.Embedding(
                vocab_size, self.embedding_size, padding_idx=self.word_pad_idx)

        self.transformer_encoder = TransformerEncoder(
                self.embedding_size, args.ff_size, args.heads,
                args.dropout, args.inter_layers)

        if self.review_encoder_name == "pv":
            pretrain_emb_path = None
            if args.pretrain_emb_dir != "":
                pretrain_emb_path = os.path.join(args.pretrain_emb_dir, "doc_emb.txt.gz")
            self.review_encoder = ParagraphVector(
                    self.word_embeddings, self.word_dists,
                    review_count, self.emb_dropout, pretrain_emb_path, fix_emb=self.fix_emb)
        elif self.review_encoder_name == "pvc":
            self.review_encoder = ParagraphVectorCorruption(
                    self.word_embeddings, self.word_dists, args.corrupt_rate, self.emb_dropout)
        elif self.review_encoder_name == "fs":
            self.review_encoder = FSEncoder(self.embedding_size, self.emb_dropout)
        else:
            self.review_encoder = AVGEncoder(self.embedding_size, self.emb_dropout)

        if args.query_encoder_name == "fs":
            self.query_encoder = FSEncoder(self.embedding_size, self.emb_dropout)
        else:
            self.query_encoder = AVGEncoder(self.embedding_size, self.emb_dropout)
        self.seg_embeddings = nn.Embedding(4, self.embedding_size, padding_idx=self.seg_pad_idx)
        #for each q,u,i
        #Q, previous purchases of u, current available reviews for i, padding value
        #self.logsoftmax = torch.nn.LogSoftmax(dim = -1)
        self.bce_logits_loss = torch.nn.BCEWithLogitsLoss(reduction='none')#by default it's mean
        self.review_embeddings = None
        if self.fix_emb:
            self.word_embeddings.weight.requires_grad = False
            self.emb_dropout = 0
            self.get_review_embeddings() #get model.review_embeddings

        self.initialize_parameters(logger) #logger
        self.to(device) #change model in place


    def load_cp(self, pt, strict=True):
        self.load_state_dict(pt['model'], strict=strict)

    def clear_review_embbeddings(self):
        #otherwise review_embeddings are always the same
        if not self.fix_emb:
            self.review_embeddings = None
            #del self.review_embeddings
            torch.cuda.empty_cache()

    def get_review_embeddings(self, batch_size=128):
        if hasattr(self, "review_embeddings") and self.review_embeddings is not None:
            return #if already computed and not deleted
        if self.review_encoder_name == "pv":
            self.review_embeddings = self.review_encoder.review_embeddings.weight
        else:
            #padded_review_words = pad(global_data.review_words, pad_id = self.word_pad_idx)
            review_count = self.review_pad_idx
            seg_count = int((review_count - 1) / batch_size) + 1
            self.review_embeddings = torch.zeros(review_count+1, self.embedding_size, device=self.device)
            #The last one is always 0
            for i in range(seg_count):
                slice_reviews = self.review_words[i*batch_size:(i+1)*batch_size]
                if self.review_encoder_name == "pvc":
                    slice_review_emb = self.review_encoder.get_para_vector(slice_reviews)
                else: #fs or avg
                    slice_rword_emb = self.word_embeddings(slice_reviews)
                    slice_review_emb = self.review_encoder(slice_rword_emb, slice_reviews.ne(self.word_pad_idx))
                self.review_embeddings[i*batch_size:(i+1)*batch_size] = slice_review_emb

    def test(self, batch_data):
        query_word_idxs = batch_data.query_word_idxs
        candi_prod_ridxs = batch_data.candi_prod_ridxs
        candi_seg_idxs = batch_data.candi_seg_idxs
        query_word_emb = self.word_embeddings(query_word_idxs)
        query_emb = self.query_encoder(query_word_emb, query_word_idxs.ne(self.word_pad_idx))
        batch_size, candi_k, candi_rcount = candi_prod_ridxs.size()
        candi_review_emb = self.review_embeddings[candi_prod_ridxs]

        #concat query_emb with pos_review_emb and candi_review_emb
        query_mask = torch.ones(batch_size, 1, dtype=torch.uint8, device=query_word_idxs.device)
        candi_prod_ridx_mask = candi_prod_ridxs.ne(self.review_pad_idx)
        candi_review_mask = torch.cat([query_mask.unsqueeze(1).expand(-1,candi_k,-1), candi_prod_ridx_mask], dim=2)
        #batch_size, 1, embedding_size
        candi_sequence_emb = torch.cat(
                (query_emb.unsqueeze(1).expand(-1, candi_k, -1).unsqueeze(2), candi_review_emb), dim=2)
        #batch_size, candi_k, max_review_count+1, embedding_size
        candi_seg_emb = self.seg_embeddings(candi_seg_idxs) #batch_size, candi_k, max_review_count+1, embedding_size
        candi_sequence_emb += candi_seg_emb

        candi_scores = self.transformer_encoder(
                candi_sequence_emb.view(batch_size*candi_k, candi_rcount+1, -1),
                candi_review_mask.view(batch_size*candi_k, candi_rcount+1))
        candi_scores = candi_scores.view(batch_size, candi_k)
        return candi_scores

    def forward(self, batch_data_arr, train_pv=True):
        loss = []
        for batch_data in batch_data_arr:
            cur_loss = self.pass_one_batch(batch_data, train_pv)
            loss.append(cur_loss)
        return sum(loss) / len(loss)

    def pass_one_batch(self, batch_data, train_pv=True):
        query_word_idxs = batch_data.query_word_idxs
        pos_prod_ridxs = batch_data.pos_prod_ridxs
        pos_seg_idxs = batch_data.pos_seg_idxs
        pos_prod_rword_idxs= batch_data.pos_prod_rword_idxs
        pos_prod_rword_masks = batch_data.pos_prod_rword_masks
        neg_prod_ridxs = batch_data.neg_prod_ridxs
        neg_seg_idxs = batch_data.neg_seg_idxs
        neg_prod_rword_idxs = batch_data.neg_prod_rword_idxs
        neg_prod_rword_masks = batch_data.neg_prod_rword_masks
        pos_prod_rword_idxs_pvc = batch_data.pos_prod_rword_idxs_pvc
        neg_prod_rword_idxs_pvc = batch_data.neg_prod_rword_idxs_pvc
        query_word_emb = self.word_embeddings(query_word_idxs)
        query_emb = self.query_encoder(query_word_emb, query_word_idxs.ne(self.word_pad_idx))
        batch_size, pos_rcount, posr_word_limit = pos_prod_rword_idxs.size()
        _, neg_k, neg_rcount = neg_prod_ridxs.size()
        posr_word_emb = self.word_embeddings(pos_prod_rword_idxs.view(-1, posr_word_limit))
        update_pos_prod_rword_masks = pos_prod_rword_masks.view(-1, posr_word_limit)
        pv_loss = None
        if "pv" in self.review_encoder_name:
            if train_pv:
                if self.review_encoder_name == "pv":
                    pos_review_emb, pos_prod_loss = self.review_encoder(
                            pos_prod_ridxs.view(-1), posr_word_emb,
                            update_pos_prod_rword_masks, self.args.neg_per_pos)
                elif self.review_encoder_name == "pvc":
                    pos_review_emb, pos_prod_loss = self.review_encoder(
                            posr_word_emb, update_pos_prod_rword_masks,
                            pos_prod_rword_idxs_pvc.view(-1, pos_prod_rword_idxs_pvc.size(-1)),
                            self.args.neg_per_pos)
                sample_count = pos_prod_ridxs.ne(self.review_pad_idx).float().sum()
                sample_count = sample_count.masked_fill(sample_count.eq(0),1)
                pv_loss = pos_prod_loss.sum() / sample_count
            else:
                if self.fix_emb:
                    pos_review_emb = self.review_embeddings[pos_prod_ridxs]
                else:
                    if self.review_encoder_name == "pv":
                        pos_review_emb = self.review_encoder.get_para_vector(pos_prod_ridxs)
                    elif self.review_encoder_name == "pvc":
                        pos_review_emb = self.review_encoder.get_para_vector(
                                #pos_prod_rword_idxs_pvc.view(-1, pos_prod_rword_idxs_pvc.size(-1)))
                                pos_prod_rword_idxs.view(-1, pos_prod_rword_idxs.size(-1)))
            if self.fix_emb:
                neg_review_emb = self.review_embeddings[neg_prod_ridxs]
            else:
                if self.review_encoder_name == "pv":
                    neg_review_emb = self.review_encoder.get_para_vector(neg_prod_ridxs)
                elif self.review_encoder_name == "pvc":
                    if not train_pv:
                        neg_prod_rword_idxs_pvc = neg_prod_rword_idxs
                    neg_review_emb = self.review_encoder.get_para_vector(
                            neg_prod_rword_idxs_pvc.view(-1, neg_prod_rword_idxs_pvc.size(-1)))
            pos_review_emb = self.dropout_layer(pos_review_emb)
            neg_review_emb = self.dropout_layer(neg_review_emb)
        else:
            negr_word_limit = neg_prod_rword_idxs.size()[-1]
            negr_word_emb = self.word_embeddings(neg_prod_rword_idxs.view(-1, negr_word_limit))
            pos_review_emb = self.review_encoder(posr_word_emb, update_pos_prod_rword_masks)
            neg_review_emb = self.review_encoder(negr_word_emb, neg_prod_rword_masks.view(-1, negr_word_limit))

        pos_review_emb = pos_review_emb.view(batch_size, pos_rcount, -1)
        neg_review_emb = neg_review_emb.view(batch_size, neg_k, neg_rcount, -1)

        #concat query_emb with pos_review_emb and neg_review_emb
        query_mask = torch.ones(batch_size, 1, dtype=torch.uint8, device=query_word_idxs.device)
        pos_review_mask = torch.cat([query_mask, pos_prod_ridxs.ne(self.review_pad_idx)], dim=1) #batch_size, 1+max_review_count
        neg_prod_ridx_mask = neg_prod_ridxs.ne(self.review_pad_idx)
        neg_review_mask = torch.cat([query_mask.unsqueeze(1).expand(-1,neg_k,-1), neg_prod_ridx_mask], dim=2)
        #batch_size, 1, embedding_size
        pos_sequence_emb = torch.cat((query_emb.unsqueeze(1), pos_review_emb), dim=1)
        pos_seg_emb = self.seg_embeddings(pos_seg_idxs) #batch_size, max_review_count+1, embedding_size
        neg_sequence_emb = torch.cat(
                (query_emb.unsqueeze(1).expand(-1, neg_k, -1).unsqueeze(2), neg_review_emb), dim=2)
        #batch_size, neg_k, max_review_count+1, embedding_size
        neg_seg_emb = self.seg_embeddings(neg_seg_idxs) #batch_size, neg_k, max_review_count+1, embedding_size
        pos_sequence_emb += pos_seg_emb
        neg_sequence_emb += neg_seg_emb

        pos_scores = self.transformer_encoder(pos_sequence_emb, pos_review_mask)
        neg_scores = self.transformer_encoder(
                neg_sequence_emb.view(batch_size*neg_k, neg_rcount+1, -1),
                neg_review_mask.view(batch_size*neg_k, neg_rcount+1))
        neg_scores = neg_scores.view(batch_size, neg_k)
        prod_mask = torch.cat([torch.ones(batch_size, 1, dtype=torch.uint8, device=query_word_idxs.device),
            neg_prod_ridx_mask.sum(-1).ne(0)], dim=-1) #batch_size, neg_k (valid products, some are padded)
        prod_scores = torch.cat([pos_scores.unsqueeze(-1), neg_scores], dim=-1)
        target = torch.cat([torch.ones(batch_size, 1, device=query_word_idxs.device),
            torch.zeros(batch_size, neg_k, device=query_word_idxs.device)], dim=-1)
        ps_loss = self.bce_logits_loss(prod_scores, target) * prod_mask.float()
        ps_loss = ps_loss.sum(-1).mean()
        loss = ps_loss + pv_loss if pv_loss is not None else ps_loss
        return loss

    def initialize_parameters(self, logger=None):
        if logger:
            logger.info(" ProductRanker initialization started.")
        if self.pretrain_emb_dir == "":
            nn.init.normal_(self.word_embeddings.weight)
        nn.init.normal_(self.seg_embeddings.weight)
        self.review_encoder.initialize_parameters(logger)
        self.query_encoder.initialize_parameters(logger)
        self.transformer_encoder.initialize_parameters(logger)
        if logger:
            logger.info(" ProductRanker initialization finished.")

