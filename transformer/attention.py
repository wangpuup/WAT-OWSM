#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright 2019 Shigeki Karita
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

"""Multi-Head Attention layer definition."""

import math
import random

import torch
from torch import nn


class AdpHeadedAttention(nn.Module):
    """Multi-Head Attention layer.

    Args:
        n_head (int): The number of heads.
        n_feat (int): The number of features.
        dropout_rate (float): Dropout rate.

    """

    def __init__(self, n_head, n_feat, dropout_rate):
        """Construct an MultiHeadedAttention object."""
        super(AdpHeadedAttention, self).__init__()
        assert n_feat % n_head == 0
        # We assume d_v always equals d_k
        self.d_k = n_feat // n_head
        self.h = n_head
        self.linear_q = nn.Linear(n_feat, n_feat)
        self.linear_k = nn.Linear(n_feat, n_feat)
        self.linear_v = nn.Linear(n_feat, n_feat)
        self.linear_out = nn.Linear(n_feat, n_feat)
        self.attn = None
        self.a_q = torch.nn.Parameter(torch.FloatTensor(torch.ones(n_feat)))
        self.dropout = nn.Dropout(p=dropout_rate)

    def forward_qkv(self, query, key, value):
        """Transform query, key and value.

        Args:
            query (torch.Tensor): Query tensor (#batch, time1, size).
            key (torch.Tensor): Key tensor (#batch, time2, size).
            value (torch.Tensor): Value tensor (#batch, time2, size).
            expand_kv (bool): Used only for partially autoregressive (PAR) decoding.

        Returns:
            torch.Tensor: Transformed query tensor (#batch, n_head, time1, d_k).
            torch.Tensor: Transformed key tensor (#batch, n_head, time2, d_k).
            torch.Tensor: Transformed value tensor (#batch, n_head, time2, d_k).

        """
        n_batch = query.size(0)

        norm_qq = torch.linalg.norm(self.linear_q.weight, ord=2, dim=1)+(random.random()*(1e-10))
        norm_kk = torch.linalg.norm(self.linear_k.weight, ord=2, dim=1)+(random.random()*(1e-10))

        q = self.linear_q(query).view(n_batch, -1, self.h * self.d_k)
        k = self.linear_k(key).view(n_batch, -1, self.h * self.d_k)
        v = self.linear_v(value).view(n_batch, -1, self.h, self.d_k)
        d_out = self.linear_q.weight.size(-1)

        a_q_max = torch.linalg.norm(self.a_q, ord=float('inf')).to(self.a_q.device)
        base_a_q = torch.ones(self.linear_q.weight.size(0)).to(self.a_q.device)*(0.001*a_q_max)
        a_q_mask = torch.gt(torch.abs(self.a_q), base_a_q).float()

        a_q_f = self.a_q * a_q_mask

        q = q * torch.unsqueeze(torch.unsqueeze(a_q_f,0),0)

        q = q/(torch.unsqueeze(torch.unsqueeze(norm_qq,0),0))
        k = k/(torch.unsqueeze(torch.unsqueeze(norm_kk,0),0))

        k =k.view(n_batch, -1, self.h, self.d_k)
        q =q.view(n_batch, -1, self.h, self.d_k)

        l_a_q = torch.linalg.norm(a_q_f, ord=1,dim=0)
        l_qk = 1.0*l_a_q

        q = q.transpose(1, 2)  # (batch, head, time1, d_k)
        k = k.transpose(1, 2)  # (batch, head, time2, d_k)
        v = v.transpose(1, 2)  # (batch, head, time2, d_k)

        return q, k, v, l_qk, a_q_f

    def forward_attention(self, value, scores, mask):
        """Compute attention context vector.

        Args:
            value (torch.Tensor): Transformed value (#batch, n_head, time2, d_k).
            scores (torch.Tensor): Attention score (#batch, n_head, time1, time2).
            mask (torch.Tensor): Mask (#batch, 1, time2) or (#batch, time1, time2).

        Returns:
            torch.Tensor: Transformed value (#batch, time1, d_model)
                weighted by the attention score (#batch, time1, time2).

        """
        n_batch = value.size(0)
        if mask is not None:
            mask = mask.unsqueeze(1).eq(0)  # (batch, 1, *, time2)
            min_value = torch.finfo(scores.dtype).min
            scores = scores.masked_fill(mask, min_value)
            self.attn = torch.softmax(scores, dim=-1).masked_fill(
                mask, 0.0
            )  # (batch, head, time1, time2)
        else:
            self.attn = torch.softmax(scores, dim=-1)  # (batch, head, time1, time2)

        p_attn = self.dropout(self.attn)
        x = torch.matmul(p_attn, value)  # (batch, head, time1, d_k)
        x = (
            x.transpose(1, 2).contiguous().view(n_batch, -1, self.h * self.d_k)
        )  # (batch, time1, d_model)

        return self.linear_out(x)  # (batch, time1, d_model)

    def forward(self, query, key, value, mask):
        """Compute scaled dot product attention.

        Args:
            query (torch.Tensor): Query tensor (#batch, time1, size).
            key (torch.Tensor): Key tensor (#batch, time2, size).
            value (torch.Tensor): Value tensor (#batch, time2, size).
            mask (torch.Tensor): Mask tensor (#batch, 1, time2) or
                (#batch, time1, time2).
            expand_kv (bool): Used only for partially autoregressive (PAR) decoding.
        When set to `True`, `Linear` layers are computed only for the first batch.
        This is useful to reduce the memory usage during decoding when the batch size is
        #beam_size x #mask_count, which can be very large. Typically, in single waveform
        inference of PAR, `Linear` layers should not be computed for all batches
        for source-attention.

        Returns:
            torch.Tensor: Output tensor (#batch, time1, d_model).

        """
        q, k, v, l_qk, a_q_f = self.forward_qkv(query, key, value)
        scores = torch.matmul(q, k.transpose(-2, -1)) / math.sqrt(self.d_k)
        return self.forward_attention(v, scores, mask), l_qk, a_q_f
