import os, sys

FALCON_DIR = os.environ.get('FALCONDIR')
sys.path.append(FALCON_DIR)

from models import *
from layers import *
from util import *

class Decoder_TacotronOneSeqwise(Decoder_TacotronOne):
    def __init__(self, in_dim, r):
        super(Decoder_TacotronOneSeqwise, self).__init__(in_dim, r)
        self.prenet = Prenet_seqwise(in_dim * r, sizes=[256, 128])


class TacotronOneSeqwise(TacotronOne):

    def __init__(self, n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025,
                 r=5, padding_idx=None, use_memory_mask=False):
        super(TacotronOneSeqwise, self).__init__(n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025,
                 r=5, padding_idx=None, use_memory_mask=False)
        self.decoder = Decoder_TacotronOneSeqwise(mel_dim, r)


class Decoder_Audiosearch(nn.Module):
    def __init__(self, in_dim, r):
        super(Decoder_Audiosearch, self).__init__()
        self.in_dim = in_dim
        self.r = r
        self.prenet = Prenet(in_dim * r, sizes=[256, 128])
        # (prenet_out + attention context) -> output
        self.attention_rnn = AttentionWrapper(
            nn.GRUCell(256 + 128, 256),
            BahdanauAttention(256)
        )
        self.memory_layer = nn.Linear(256, 256, bias=False)
        self.project_to_decoder_in = nn.Linear(512, 256)

        self.decoder_rnns = nn.ModuleList(
            [nn.GRUCell(256, 256) for _ in range(2)])

        self.proj_to_mel = nn.Linear(256, in_dim * r)
        self.max_decoder_steps = 200

    def forward(self, encoder_outputs, inputs=None, memory_lengths=None):
        """
        Decoder forward step.
        If decoder inputs are not given (e.g., at testing time), as noted in
        Tacotron paper, greedy decoding is adapted.
        Args:
            encoder_outputs: Encoder outputs. (B, T_encoder, dim)
            inputs: Decoder inputs. i.e., mel-spectrogram. If None (at eval-time),
              decoder outputs are used as decoder inputs.
            memory_lengths: Encoder output (memory) lengths. If not None, used for
              attention masking.
        """
        B = encoder_outputs.size(0)
      
        processed_memory = self.memory_layer(encoder_outputs)
        if memory_lengths is not None:
            mask = get_mask_from_lengths(processed_memory, memory_lengths)
        else:
            mask = None

        # Run greedy decoding if inputs is None
        greedy = inputs is None

        if inputs is not None:
            #print("Shape of inputs and r: ", inputs.shape, self.r)
            # Grouping multiple frames if necessary
            if inputs.size(-1) == self.in_dim:
                inputs = inputs.view(B, inputs.size(1) // self.r, -1)
            assert inputs.size(-1) == self.in_dim * self.r
            T_decoder = inputs.size(1)

        # go frames
        initial_input = Variable(
            encoder_outputs.data.new(B, self.in_dim * self.r).zero_())

        # Init decoder states
        attention_rnn_hidden = Variable(
            encoder_outputs.data.new(B, 256).zero_())
        decoder_rnn_hiddens = [Variable(
            encoder_outputs.data.new(B, 256).zero_())
            for _ in range(len(self.decoder_rnns))]
        current_attention = Variable(
            encoder_outputs.data.new(B, 256).zero_())

        # Time first (T_decoder, B, in_dim)
        if inputs is not None:
            inputs = inputs.transpose(0, 1)

        outputs = []
        alignments = []

        t = 0
        current_input = initial_input
        while True:
            if t > 0:
                current_input = outputs[-1] if greedy else inputs[t - 1]
            # Prenet
            ####### Sai Krishna Rallabandi 15 June 2019 #####################
            #print("Shape of input to the decoder prenet: ", current_input.shape)
            if len(current_input.shape) < 3:
               current_input = current_input.unsqueeze(1)
            #################################################################
 
            current_input = self.prenet(current_input)

            # Attention RNN
            attention_rnn_hidden, current_attention, alignment = self.attention_rnn(
                current_input, current_attention, attention_rnn_hidden,
                encoder_outputs, processed_memory=processed_memory, mask=mask)

            # Concat RNN output and attention context vector
            decoder_input = self.project_to_decoder_in(
                torch.cat((attention_rnn_hidden, current_attention), -1))

            # Pass through the decoder RNNs
            for idx in range(len(self.decoder_rnns)):
                decoder_rnn_hiddens[idx] = self.decoder_rnns[idx](
                    decoder_input, decoder_rnn_hiddens[idx])
                # Residual connectinon
                decoder_input = decoder_rnn_hiddens[idx] + decoder_input

            output = decoder_input
            output = self.proj_to_mel(output)

            outputs += [output]
            alignments += [alignment]

            t += 1

            if greedy:
                if t > 1 and is_end_of_frames(output):
                    break
                elif t > self.max_decoder_steps:
                    print("Warning! doesn't seems to be converged")
                    break
            else:
                if t >= T_decoder:
                    break

        assert greedy or len(outputs) == T_decoder

        # Back to batch first
        alignments = torch.stack(alignments).transpose(0, 1)
        outputs = torch.stack(outputs).transpose(0, 1).contiguous()

        return outputs, alignments

# Type: Acquisition_CodeBorrowed Source: https://github.com/r9y9/tacotron_pytorch/blob/62db7217c10da3edb34f67b185cc0e2b04cdf77e/tacotron_pytorch/tacotron.py#L273
def is_end_of_frames(output, eps=0.2):
    return (output.data <= eps).all()

# https://pytorch.org/tutorials/beginner/transformer_tutorial.html
class PositionalEncoding(nn.Module):

    def __init__(self, d_model, dropout=0.1, max_len=5000):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)

        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).transpose(0, 1)
        self.register_buffer('pe', pe)

    def forward(self, x):
        x = x + self.pe[:x.size(0), :]
        return self.dropout(x)

class TacotronOneSelfAttention(TacotronOneSeqwise):

    def __init__(self, n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025,
                 r=5, num_attention_heads = 6, num_encoder_layers = 6, padding_idx=None, use_memory_mask=False):
        super(TacotronOneSelfAttention, self).__init__(n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025,
                 r=5, padding_idx=None, use_memory_mask=False)

        self.dropout = 0.2
        self.activation = "relu"
        self.num_encoder_layers = num_encoder_layers
        self.src_mask = None
        self.num_attention_heads = num_attention_heads
        self.embedding_dim = embedding_dim
        self.pos_encoder = PositionalEncoding(embedding_dim, self.dropout)
        encoder_layers = nn.TransformerEncoderLayer(embedding_dim, self.num_attention_heads, 128, self.dropout)
        encoder_norm = nn.LayerNorm(embedding_dim)
        self.transformer_encoder = nn.TransformerEncoder(encoder_layers, self.num_encoder_layers, encoder_norm)
        initrange = 0.1
        self.embedding.weight.data.uniform_(-initrange, initrange)

    def _generate_square_subsequent_mask(self, sz):
        mask = (torch.triu(torch.ones(sz, sz)) == 1).transpose(0, 1)
        mask = mask.float().masked_fill(mask == 0, float('-inf')).masked_fill(mask == 1, float(0.0))
        return mask


    def forward(self, inputs, targets=None, input_lengths=None):

        B = inputs.size(0)
        T = inputs.size(1)

        # Embeddings for text
        inputs = self.embedding(inputs)

        # Generate mask Transformer takes (T,B,C)
        inputs = inputs.transpose(0,1)

        if self.src_mask is None or self.src_mask.size(0) != T:
            mask = self._generate_square_subsequent_mask(T).cuda()
            self.src_mask = mask

        inputs = inputs  * math.sqrt(self.embedding_dim)
        inputs = self.pos_encoder(inputs)
        #decoder_inputs = self.transformer_encoder(inputs, self.src_mask) 
        decoder_inputs = self.transformer_encoder(inputs)
        decoder_inputs = decoder_inputs.transpose(0,1)
 
        if isnan(decoder_inputs):
           print("NANs in decoder inputs")
           sys.exit()
        #else:
        #   print(decoder_inputs)
        

        # Decoder
        input_lengths = None
        mel_outputs, alignments = self.decoder(decoder_inputs, targets, memory_lengths=input_lengths)
        mel_outputs = mel_outputs.view(B, -1, self.mel_dim)

        # PostNet
        linear_outputs = self.postnet(mel_outputs)
        linear_outputs = self.last_linear(linear_outputs)
        
        return mel_outputs, linear_outputs, alignments

def isnan(x):
    return (x != x).any()


class TacotronOneSeqwiseAudiosearch(TacotronOne):

    def __init__(self, n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025,
                 r=5, padding_idx=None, use_memory_mask=False):
        super(TacotronOneSeqwiseAudiosearch, self).__init__(n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025,
                 r=5, padding_idx=None, use_memory_mask=False)
 
        self.decoder_LSTM = nn.LSTM(256, 128, batch_first=True, bidirectional = True)
        self.decoderfc_search = nn.Linear(256, 2)

        self.decoder = Decoder_Audiosearch(256, r)
        self.decoderfc_reconstruction = SequenceWise(nn.Linear(256, n_vocab))

    def forward(self, inputs, targets):

        B = inputs.size(0)

        inputs = self.embedding(inputs)
        targets = self.embedding(targets.long())
        encoder_outputs = self.encoder(inputs)

        outputs, _ = self.decoder_LSTM(encoder_outputs)
        outputs = outputs[:, 0, :]

        outputs_reconstructed, alignments  = self.decoder(encoder_outputs, targets)

        return self.decoderfc_search(outputs), self.decoderfc_reconstruction(outputs_reconstructed.view(B, -1, 256))




# https://github.com/mkotha/WaveRNN/blob/master/layers/downsampling_encoder.py
class DownsamplingEncoder(nn.Module):
    """
        Input: (N, samples_i) numeric tensor
        Output: (N, samples_o, channels) numeric tensor
    """
    def __init__(self, channels, layer_specs):
        super().__init__()

        self.convs_wide = nn.ModuleList()
        self.convs_1x1 = nn.ModuleList()
        self.layer_specs = layer_specs
        prev_channels = channels
        total_scale = 1
        pad_left = 0
        self.skips = []
        for stride, ksz, dilation_factor in layer_specs:
            conv_wide = nn.Conv1d(prev_channels, 2 * channels, ksz, stride=stride, dilation=dilation_factor)
            wsize = 2.967 / math.sqrt(ksz * prev_channels)
            conv_wide.weight.data.uniform_(-wsize, wsize)
            conv_wide.bias.data.zero_()
            self.convs_wide.append(conv_wide)

            conv_1x1 = nn.Conv1d(channels, channels, 1)
            conv_1x1.bias.data.zero_()
            self.convs_1x1.append(conv_1x1)

            prev_channels = channels
            skip = (ksz - stride) * dilation_factor
            pad_left += total_scale * skip
            self.skips.append(skip)
            total_scale *= stride
        self.pad_left = pad_left
        self.total_scale = total_scale

        self.final_conv_0 = nn.Conv1d(channels, channels, 1)
        self.final_conv_0.bias.data.zero_()
        self.final_conv_1 = nn.Conv1d(channels, channels, 1)

    def forward(self, samples):
        x = samples.transpose(1,2) #.unsqueeze(1)
        #print("Shape of input: ", x.shape)
        for i, stuff in enumerate(zip(self.convs_wide, self.convs_1x1, self.layer_specs, self.skips)):
            conv_wide, conv_1x1, layer_spec, skip = stuff
            stride, ksz, dilation_factor = layer_spec
            #print(i, "Stride, ksz, DF and shape of input: ", stride, ksz, dilation_factor, x.shape)
            x1 = conv_wide(x)
            x1_a, x1_b = x1.split(x1.size(1) // 2, dim=1)
            x2 = torch.tanh(x1_a) * torch.sigmoid(x1_b)
            x3 = conv_1x1(x2)
            if i == 0:
                x = x3
            else:
                x = x3 + x[:, :, skip:skip+x3.size(2)*stride].view(x.size(0), x3.size(1), x3.size(2), -1)[:, :, :, -1]
        x = self.final_conv_1(F.relu(self.final_conv_0(x)))
        return x.transpose(1, 2)


# https://github.com/jefflai108/Contrastive-Predictive-Coding-PyTorch/blob/a9dab4e759aaa68dce1b1ada46a8035076ba3296/src/model/model.py#L245
# https://github.com/ssp573/Contrastive-Predictive-Coding
# https://github.com/davidtellez/contrastive-predictive-coding
class CPCBaseline(TacotronOne):

    def __init__(self, n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025,
                 r=5, padding_idx=None, use_memory_mask=False):
        super(CPCBaseline, self).__init__(n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025,
                 r=5, padding_idx=None, use_memory_mask=False)

        encoder_layers = [
            (2, 4, 1),
            (2, 4, 1),
            (2, 4, 1),
            (1, 4, 1),
            (2, 4, 1),
            (1, 4, 1),
            (2, 4, 1),
            (1, 4, 1),

            ]
        self.encoder = DownsamplingEncoder(embedding_dim, encoder_layers)
        self.decoder_fc = nn.Linear(embedding_dim, embedding_dim, bias=False)
        self.decoder_lstm = nn.GRU(embedding_dim, embedding_dim, batch_first = True)
        self.lsoftmax = nn.LogSoftmax(dim=-1)

    def forward(self, inputs):

        encoded = self.encoder(inputs.unsqueeze(-1))
        latents, hidden = self.decoder_lstm(encoded)
        #print("Shape of latents: ", latents.shape)
        z = latents[:,-1,:]
        #print("Shape of z: ", z.shape)
        predictions = self.decoder_fc(z)
        #print("Shape of predictions: ", predictions.shape)
        total = torch.mm(predictions, predictions.transpose(0,1))
        nce_loss = torch.sum(torch.diag(self.lsoftmax(total)))
        return -1 * nce_loss


# https://github.com/r9y9/wavenet_vocoder/blob/master/wavenet_vocoder/upsample.py
class UpsampleNetwork(nn.Module):
    def __init__(self, upsample_scales, upsample_activation="none",
                 upsample_activation_params={}, mode="nearest",
                 freq_axis_kernel_size=1, cin_pad=0, cin_channels=80):
        super(UpsampleNetwork, self).__init__()
        self.up_layers = nn.ModuleList()
        total_scale = np.prod(upsample_scales)
        self.indent = cin_pad * total_scale
        for scale in upsample_scales:
            freq_axis_padding = (freq_axis_kernel_size - 1) // 2
            k_size = (freq_axis_kernel_size, scale * 2 + 1)
            padding = (freq_axis_padding, scale)
            stretch = Stretch2d(scale, 1, mode)
            conv = nn.Conv2d(1, 1, kernel_size=k_size, padding=padding, bias=False)
            conv.weight.data.fill_(1. / np.prod(k_size))
            conv = nn.utils.weight_norm(conv)
            self.up_layers.append(stretch)
            self.up_layers.append(conv)
            if upsample_activation != "none":
                nonlinear = _get_activation(upsample_activation)
                self.up_layers.append(nonlinear(**upsample_activation_params))

    def forward(self, c):
   
        c = c.transpose(1,2)

        # B x 1 x C x T
        c = c.unsqueeze(1)
        for f in self.up_layers:
            c = f(c)
        # B x C x T
        c = c.squeeze(1)

        if self.indent > 0:
            c = c[:, :, self.indent:-self.indent]
        return c.transpose(1,2)


class Stretch2d(nn.Module):
    def __init__(self, x_scale, y_scale, mode="nearest"):
        super(Stretch2d, self).__init__()
        self.x_scale = x_scale
        self.y_scale = y_scale
        self.mode = mode

    def forward(self, x):
        return F.interpolate(
            x, scale_factor=(self.y_scale, self.x_scale), mode=self.mode)


def _get_activation(upsample_activation):
    nonlinear = getattr(nn, upsample_activation)
    return nonlinear


# https://github.com/mkotha/WaveRNN/blob/master/layers/upsample.py
class UpsampleNetwork_kotha(nn.Module):
    """
    Input: (N, C, L) numeric tensor
    Output: (N, C, L1) numeric tensor
    """
    def __init__(self, feat_dims, upsample_scales):
        super().__init__()
        self.up_layers = nn.ModuleList()
        self.scales = upsample_scales
        for scale in upsample_scales:
            conv = nn.Conv2d(1, 1,
                    kernel_size = (1, 2 * scale - 1))
            conv.bias.data.zero_()
            self.up_layers.append(conv)

    def forward(self, mels):
        n = mels.size(0)
        feat_dims = mels.size(1)
        x = mels.unsqueeze(1)
        for (scale, up) in zip(self.scales, self.up_layers):
            x = up(x.unsqueeze(-1).expand(-1, -1, -1, -1, scale).reshape(n, 1, feat_dims, -1))
        return x.squeeze(1)[:, :, 1:-1]


class MelVQVAEBaseline(TacotronOne):

    def __init__(self, n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025,
                 r=5, padding_idx=None, use_memory_mask=False):
        super(MelVQVAEBaseline, self).__init__(n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025,
                 r=5, padding_idx=None, use_memory_mask=False)

        # Stride, KernelSize, DilationFactor
        encoder_layers = [
            (2, 4, 1),
            (2, 4, 1),
            (2, 4, 1),
            (1, 4, 1),
            (2, 4, 1),
            (1, 4, 1),
            #(2, 4, 1),
            #(1, 4, 1),
            #(2, 4, 1),
            #(1, 4, 1),
            ]
        self.encoder = DownsamplingEncoder(mel_dim, encoder_layers)
        self.quantizer = quantizer_kotha(n_channels=1, n_classes=200, vec_len=80)
        self.upsample_scales = [2,4,2,4]
        self.upsample_network = UpsampleNetwork(self.upsample_scales)

        self.decoder_lstm = nn.LSTM(80, 128, bidirectional=True, batch_first=True)
        self.decoder_fc = nn.Linear(80,256)
        self.mel_dim = mel_dim  
        self.decoder = Decoder_TacotronOneSeqwise(mel_dim, r)

    def forward(self, mel):
        B = mel.shape[0]
        encoded = self.encoder(mel)
        #print("Shape of encoded: ", encoded.shape)
        quantized, vq_penalty, encoder_penalty, entropy = self.quantizer(encoded.unsqueeze(2))
        #print("Shape of quantized: ", quantized.shape)
        quantized = quantized.squeeze(2)
        #upsampled = self.upsample_network(quantized)
        #outputs, hidden = self.decoder_lstm(upsampled)
        #outputs =  self.decoder_fc(outputs)
        decoder_input = torch.tanh(self.decoder_fc(quantized))
        mel_outputs, alignments = self.decoder(decoder_input, mel)
        mel_outputs = mel_outputs.view(B, -1, self.mel_dim)
        #print("Shape of outputs: ", mel_outputs.shape)

        return mel_outputs, alignments, vq_penalty.mean(), encoder_penalty.mean(), entropy



class MelVQVAEv2(TacotronOne):

    def __init__(self, n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025,
                 r=5, padding_idx=None, use_memory_mask=False):
        super(MelVQVAEv2, self).__init__(n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025,
                 r=5, padding_idx=None, use_memory_mask=False)

        # Stride, KernelSize, DilationFactor
        encoder_layers = [
            (2, 4, 1),
            (2, 4, 1),
            (2, 4, 1),
            (1, 4, 1),
            (2, 4, 1),
            (1, 4, 1),
            ]
        self.encoder = DownsamplingEncoder(mel_dim, encoder_layers)
        self.quantizer = quantizer_kotha(n_channels=1, n_classes=200, vec_len=80, normalize=True)
        self.upsample_scales = [2,4,2,4]
        self.upsample_network = UpsampleNetwork(self.upsample_scales)

        self.decoder_lstm = nn.LSTM(80, 128, bidirectional=True, batch_first=True)
        self.decoder_fc = nn.Linear(80,256)
        self.mel_dim = mel_dim  
        self.decoder = Decoder_TacotronOneSeqwise(mel_dim, r)

    def forward(self, mel, input_lengths = None):
        B = mel.shape[0]
        encoded = self.encoder(mel)
        quantized, vq_penalty, encoder_penalty, entropy = self.quantizer(encoded.unsqueeze(2))
        quantized = quantized.squeeze(2)

        decoder_input = torch.tanh(self.decoder_fc(quantized))
        mel_outputs, alignments = self.decoder(decoder_input, mel, memory_lengths=input_lengths)
        mel_outputs = mel_outputs.view(B, -1, self.mel_dim)

        linear_outputs = self.postnet(mel_outputs)
        linear_outputs = self.last_linear(linear_outputs)

        return mel_outputs, linear_outputs, alignments, vq_penalty.mean(), encoder_penalty.mean(), entropy

    def forward_eval(self, mel, input_lengths = None):
        B = mel.shape[0]
        encoded = self.encoder(mel)
        print("Shape of encoded: ", encoded.shape)
        quantized, vq_penalty, encoder_penalty, entropy = self.quantizer(encoded.unsqueeze(2))
        print("Shape of quantized: ", quantized.shape)
        quantized = quantized.squeeze(2)
        decoder_input = torch.tanh(self.decoder_fc(quantized))
        mel = None
        mel_outputs, alignments = self.decoder(decoder_input, mel, memory_lengths=input_lengths)
        print("Shape of mel outputs: ", mel_outputs.shape)
        mel_outputs = mel_outputs.view(B, -1, self.mel_dim)

        linear_outputs = self.postnet(mel_outputs)
        linear_outputs = self.last_linear(linear_outputs)

        return mel_outputs, linear_outputs, alignments, vq_penalty.mean(), encoder_penalty.mean(), entropy


class MelVQVAEv3(TacotronOne):

    def __init__(self, n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025,
                 r=5, padding_idx=None, use_memory_mask=False):
        super(MelVQVAEv3, self).__init__(n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025,
                 r=5, padding_idx=None, use_memory_mask=False)
        
        # Stride, KernelSize, DilationFactor
        encoder_layers = [
            (2, 4, 1),
            (2, 4, 1),
            (2, 4, 1),
            (1, 4, 1),
            (2, 4, 1),
            (1, 4, 1),
            ]
        self.encoder_d = DownsamplingEncoder(mel_dim, encoder_layers)
        self.quantizer = quantizer_kotha(n_channels=1, n_classes=200, vec_len=80, normalize=True)
        self.upsample_scales = [2,4,2,4]
        self.upsample_network = UpsampleNetwork(self.upsample_scales)
        
        self.decoder_lstm = nn.LSTM(256, 128, bidirectional=True, batch_first=True)
        #self.decoder_fc = nn.Linear(80,256)
        self.mel_dim = mel_dim  
        self.decoder = Decoder_TacotronOneSeqwise(mel_dim, r)

        self.quantized2encoder = nn.Linear(80, 256)
        
    def forward(self, mel, input_lengths = None):
        B = mel.shape[0]
        encoded = self.encoder_d(mel)
        quantized, vq_penalty, encoder_penalty, entropy = self.quantizer(encoded.unsqueeze(2))
        quantized = quantized.squeeze(2)
        quantized = torch.tanh(self.quantized2encoder(quantized))
        decoder_input = self.encoder(quantized)         

        #decoder_input = torch.tanh(self.decoder_fc(quantized))
        mel_outputs, alignments = self.decoder(decoder_input, mel, memory_lengths=input_lengths)
        mel_outputs = mel_outputs.view(B, -1, self.mel_dim)

        linear_outputs = self.postnet(mel_outputs)
        linear_outputs = self.last_linear(linear_outputs)
       
        return mel_outputs, linear_outputs, alignments, vq_penalty.mean(), encoder_penalty.mean(), entropy

    def forward_eval(self, mel, input_lengths = None):
        B = mel.shape[0]
        encoded = self.encoder_d(mel)
        print("Shape of encoded: ", encoded.shape)
        quantized, vq_penalty, encoder_penalty, entropy = self.quantizer(encoded.unsqueeze(2))
        print("Shape of quantized: ", quantized.shape)
        quantized = quantized.squeeze(2)
        quantized = torch.tanh(self.quantized2encoder(quantized))
        decoder_input = self.encoder(quantized)         

        mel = None
        mel_outputs, alignments = self.decoder(decoder_input, mel, memory_lengths=input_lengths)
        print("Shape of mel outputs: ", mel_outputs.shape)
        mel_outputs = mel_outputs.view(B, -1, self.mel_dim)
        
        linear_outputs = self.postnet(mel_outputs)
        linear_outputs = self.last_linear(linear_outputs)

        return mel_outputs, linear_outputs, alignments, vq_penalty.mean(), encoder_penalty.mean(), entropy



# Lets add 1x1 convlution before LSTM
class WaveLSTM12b(TacotronOne):

    def __init__(self, n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025, logits_dim=30,
                 r=5, padding_idx=None, use_memory_mask=False):
        super(WaveLSTM12b, self).__init__(n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025,
                 r=5, padding_idx=None, use_memory_mask=False)

        self.upsample_scales = [2,4,5,5]
        self.upsample_network = UpsampleNetwork(self.upsample_scales)

        self.logits_dim = logits_dim
        self.joint_encoder = nn.LSTM(81, 256, batch_first=True)
        self.hidden2linear =  SequenceWise(nn.Linear(256, 64))
        self.mels2conv = nn.Linear(81,81)
        self.conv_1x1 = nn.Conv1d(81, 81, 1)
        self.conv_1x1.bias.data.zero_()
        self.linear2logits =  SequenceWise(nn.Linear(64, self.logits_dim))


class MelVQVAEv4(WaveLSTM12b):

    def __init__(self, n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025, logits_dim=30,
                 r=5, padding_idx=None, use_memory_mask=False):
        super(MelVQVAEv4, self).__init__(n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025,
                 r=5, padding_idx=None, use_memory_mask=False)

        self.quantizer = quantizer_kotha(n_channels=1, n_classes=200, vec_len=80, normalize=False)
        self.joint_encoder = nn.LSTM(64, 256, batch_first=True)
        self.mels2encoderinput = nn.Linear(81,64)
        self.mels2conv = nn.Linear(80,64)
        self.conv_1x1 = nn.Conv1d(64, 64, 1)
        self.conv_1x1.bias.data.zero_()
        self.bn = nn.BatchNorm1d(64, momentum=0.99, eps=1e-3)

    def forward(self, mels, x):

        B = mels.size(0)

        # Get phones
        quantized, vq_penalty, encoder_penalty, entropy = self.quantizer(mels.unsqueeze(2))
        quantized = quantized.squeeze(2)
        quantized = torch.tanh(self.mels2conv(quantized))
        quantized = self.conv_1x1(quantized.transpose(1,2)) #.transpose(1,2)
        quantized = self.bn(quantized).transpose(1,2)

        # Upsample This is wrong
        quantized = self.upsample_network(mels)

        # Adjust inputs
        quantized = quantized[:,:-1,:]
        inp = x[:, :-1].unsqueeze(-1)

        # Prepare inputs for LSTM
        melsNx = torch.cat([quantized, inp], dim=-1)
        melsNx = torch.tanh(self.mels2encoderinput(melsNx))

        # Feed to encoder
        self.joint_encoder.flatten_parameters()
        outputs, hidden = self.joint_encoder(melsNx)
        
        # get logits
        logits = torch.tanh(self.hidden2linear(outputs))
        #return self.linear2logits(logits), x[:,1:].unsqueeze(-1), logits.new(1).zero_(), logits.new(1).zero_(), 0

        return self.linear2logits(logits), x[:,1:].unsqueeze(-1), vq_penalty.mean(), encoder_penalty.mean(), entropy


    def forward_eval(self, mels, log_scale_min=-50.0):
          
        B = mels.size(0) 

        # Get phones
        quantized, vq_penalty, encoder_penalty, entropy = self.quantizer(mels.unsqueeze(2))
        quantized = quantized.squeeze(2)
        quantized = torch.tanh(self.mels2conv(quantized))
        quantized = self.conv_1x1(quantized.transpose(1,2)) #.transpose(1,2)
        quantized = self.bn(quantized).transpose(1,2)
 
        mels = self.upsample_network(mels)
        T = mels.size(1)

        current_input = torch.zeros(mels.shape[0], 1).cuda()
        hidden = None
        output = []
 
 
        for i in range(T):

           # Concatenate mel and coarse_float
           m = mels[:, i,:]
           inp = torch.cat([m , current_input], dim=-1).unsqueeze(1)

           inp = torch.tanh(self.mels2encoderinput(inp))

           # Get logits
           self.joint_encoder.flatten_parameters()
           outputs, hidden = self.joint_encoder(inp, hidden)
 
           logits = torch.tanh(self.hidden2linear(outputs))
           logits = self.linear2logits(logits)

           # Sample the next input
           sample = sample_from_discretized_mix_logistic(
                        logits.view(B, -1, 1), log_scale_min=log_scale_min)
 
           output.append(sample.data)
           current_input = sample
 
           if i%10000 == 1:
              print("  Predicted sample at timestep ", i, "is :", sample, " Number of steps: ", T)
 
    
        output = torch.stack(output, dim=0)
        print("Shape of output: ", output.shape)
        return output.cpu().numpy(), entropy
 
 
# Lets add a speaker LSTM
class MelVQVAEv4b(WaveLSTM12b):

    def __init__(self, n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025, logits_dim=30,
                 r=5, padding_idx=None, use_memory_mask=False):
        super(MelVQVAEv4b, self).__init__(n_vocab, embedding_dim=256, mel_dim=80, linear_dim=1025,
                 r=5, padding_idx=None, use_memory_mask=False)

        self.quantizer = quantizer_kotha(n_channels=1, n_classes=200, vec_len=80, normalize=False)
        self.speaker_fc = nn.Linear(80,64)
        self.speaker_lstm = nn.LSTM(64, 32, bidirectional=True, batch_first=True)
        self.joint_encoder = nn.LSTM(64, 256, batch_first=True)
        self.mels2conv = nn.Linear(80,64)
        self.conv_1x1 = nn.Conv1d(64, 64, 1)
        self.conv_1x1.bias.data.zero_()
        self.bn = nn.BatchNorm1d(64, momentum=0.99, eps=1e-3)
        self.mels2encoderinput = nn.Linear(145,64)


    def forward(self, mels, x):
           
        B = mels.size(0)
    
        # Get phones
        quantized, vq_penalty, encoder_penalty, entropy = self.quantizer(mels.unsqueeze(2))
        quantized = quantized.squeeze(2)
        quantized = torch.tanh(self.mels2conv(quantized))
        quantized = self.conv_1x1(quantized.transpose(1,2)) #.transpose(1,2)
        quantized = self.bn(quantized).transpose(1,2)

        # Get speaker
        speaker_stuff = torch.tanh(self.speaker_fc(mels))
        spk_hidden, _ = self.speaker_lstm(speaker_stuff)
        spk_hidden = spk_hidden[:,-1,:]

        # Upsample This is wrong
        quantized = self.upsample_network(mels)

        # Adjust inputs
        quantized = quantized[:,:-1,:]
        inp = x[:, :-1].unsqueeze(-1)
        spk_hidden = spk_hidden.unsqueeze(1).expand(B, quantized.shape[1], -1)
        #print("Shape of quantized, inp and spk_hidden: ", quantized.shape, inp.shape, spk_hidden.shape)

        # Prepare inputs for LSTM
        melsNx = torch.cat([quantized, inp, spk_hidden], dim=-1)
        melsNx = torch.tanh(self.mels2encoderinput(melsNx))

        # Feed to encoder
        self.joint_encoder.flatten_parameters()
        outputs, hidden = self.joint_encoder(melsNx)

        # get logits
        logits = torch.tanh(self.hidden2linear(outputs))
        #return self.linear2logits(logits), x[:,1:].unsqueeze(-1), logits.new(1).zero_(), logits.new(1).zero_(), 0

        return self.linear2logits(logits), x[:,1:].unsqueeze(-1), vq_penalty.mean(), encoder_penalty.mean(), entropy
           

