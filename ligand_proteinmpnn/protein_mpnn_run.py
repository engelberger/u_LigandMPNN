import argparse
import os.path

def main(args):

    import json, time, os, sys, glob
    import shutil
    import warnings
    import numpy as np
    import torch
    from torch import optim
    from torch.utils.data import DataLoader
    from torch.utils.data.dataset import random_split, Subset
    import copy
    import torch.nn as nn
    import torch.nn.functional as F
    import random
    import os.path
    from protein_mpnn_utils import loss_nll, loss_smoothed, gather_edges, gather_nodes, gather_nodes_t, cat_neighbors_nodes, _scores, _S_to_seq, tied_featurize, parse_PDB
    from protein_mpnn_utils import StructureDataset, StructureDatasetPDB, ProteinMPNN
    
    folder_for_outputs = args.out_folder
    
    NUM_BATCHES = args.num_seq_per_target//args.batch_size
    BATCH_COPIES = args.batch_size
    temperatures = [float(item) for item in args.sampling_temp.split()]
    omit_AAs_list = args.omit_AAs
    alphabet = 'ACDEFGHIKLMNPQRSTVWYX'
    
    omit_AAs_np = np.array([AA in omit_AAs_list for AA in alphabet]).astype(np.float32)
    device = torch.device("cuda:0" if (torch.cuda.is_available()) else "cpu")
    
    if os.path.isfile(args.chain_id_jsonl):
        with open(args.chain_id_jsonl, 'r') as json_file:
            json_list = list(json_file)
        for json_str in json_list:
            chain_id_dict = json.loads(json_str)
    else:
        chain_id_dict = None
        print(40*'-')
        print('chain_id_jsonl is NOT loaded')
        
    if os.path.isfile(args.fixed_positions_jsonl):
        with open(args.fixed_positions_jsonl, 'r') as json_file:
            json_list = list(json_file)
        for json_str in json_list:
            fixed_positions_dict = json.loads(json_str)
    else:
        print(40*'-')
        print('fixed_positions_jsonl is NOT loaded')
        fixed_positions_dict = None
    
    
    if os.path.isfile(args.pssm_jsonl):
        with open(args.pssm_jsonl, 'r') as json_file:
            json_list = list(json_file)
        pssm_dict = {}
        for json_str in json_list:
            pssm_dict.update(json.loads(json_str))
    else:
        print(40*'-')
        print('pssm_jsonl is NOT loaded')
        pssm_dict = None
    
    
    if os.path.isfile(args.omit_AA_jsonl):
        with open(args.omit_AA_jsonl, 'r') as json_file:
            json_list = list(json_file)
        for json_str in json_list:
            omit_AA_dict = json.loads(json_str)
    else:
        print(40*'-')
        print('omit_AA_jsonl is NOT loaded')
        omit_AA_dict = None
    
    
    if os.path.isfile(args.bias_AA_jsonl):
        with open(args.bias_AA_jsonl, 'r') as json_file:
            json_list = list(json_file)
        for json_str in json_list:
            bias_AA_dict = json.loads(json_str)
    else:
        print(40*'-')
        print('bias_AA_jsonl is NOT loaded')
        bias_AA_dict = None
    
    
    if os.path.isfile(args.tied_positions_jsonl):
        with open(args.tied_positions_jsonl, 'r') as json_file:
            json_list = list(json_file)
        for json_str in json_list:
            tied_positions_dict = json.loads(json_str)
    else:
        print(40*'-')
        print('tied_positions_jsonl is NOT loaded')
        tied_positions_dict = None
    
    
    print(40*'-')
    bias_AAs_np = np.zeros(len(alphabet))
    if bias_AA_dict:
            for n, AA in enumerate(alphabet):
                    if AA in list(bias_AA_dict.keys()):
                            bias_AAs_np[n] = bias_AA_dict[AA]
    
    if args.pdb_path:
        if args.ligand_params_path:
            pdb_dict_list = parse_PDB(args.pdb_path, {args.pdb_path: [args.ligand_params_path]})
        else:
            pdb_dict_list = parse_PDB(args.pdb_path)
        dataset_valid = StructureDatasetPDB(pdb_dict_list, truncate=None, max_length=args.max_length)
        all_chain_list = [item[-1:] for item in list(pdb_dict_list[0]) if item[:9]=='seq_chain'] #['A','B', 'C',...]
        if args.pdb_path_chains:
            designed_chain_list = [str(item) for item in args.pdb_path_chains.split()]
        else:
            designed_chain_list = all_chain_list
        fixed_chain_list = [letter for letter in all_chain_list if letter not in designed_chain_list]
        chain_id_dict = {}
        chain_id_dict[pdb_dict_list[0]['name']]= (designed_chain_list, fixed_chain_list)
    else:
        dataset_valid = StructureDataset(args.jsonl_path, truncate=None, max_length=args.max_length)

    print(40*'-') 
    model = ProteinMPNN(num_letters=21, node_features=args.hidden_dim, edge_features=args.hidden_dim, hidden_dim=args.hidden_dim, num_encoder_layers=args.num_layers, num_decoder_layers=args.num_layers, augment_eps=args.backbone_noise, k_neighbors=args.num_connections, device=device)
    model.to(device)
    checkpoint = torch.load(args.checkpoint_path, map_location=device)
    model.load_state_dict(checkpoint['model_state_dict'])
    model.eval()
    
    # Build paths for experiment
    base_folder = folder_for_outputs
    if base_folder[-1] != '/':
        base_folder = base_folder + '/'
    if not os.path.exists(base_folder):
        os.makedirs(base_folder)
    
    if not os.path.exists(base_folder + 'seqs'):
        os.makedirs(base_folder + 'seqs')
    
    if args.save_score:
        if not os.path.exists(base_folder + 'scores'):
            os.makedirs(base_folder + 'scores')

    if args.score_only:
        if not os.path.exists(base_folder + 'score_only'):
            os.makedirs(base_folder + 'score_only')
   
 
    if args.save_probs:
        if not os.path.exists(base_folder + 'probs'):
            os.makedirs(base_folder + 'probs') 
    
    # Timing
    start_time = time.time()
    total_residues = 0
    protein_list = []
    total_step = 0
    # Validation epoch
    with torch.no_grad():
        test_sum, test_weights = 0., 0.
        #print('Generating sequences...')
        for ix, protein in enumerate(dataset_valid):
            score_list = []
            all_probs_list = []
            all_log_probs_list = []
            S_sample_list = []
            batch_clones = [copy.deepcopy(protein) for i in range(BATCH_COPIES)]
            Z, Z_m, Z_t, X, X_m, Y, Y_m, S, mask, lengths, chain_M, chain_encoding_all, chain_list_list, visible_list_list, masked_list_list, masked_chain_length_list_list, chain_M_pos, omit_AA_mask, residue_idx, dihedral_mask, tied_pos_list_of_lists_list, pssm_coef, pssm_bias, pssm_log_odds_all, tied_beta = tied_featurize(batch_clones, device, chain_id_dict, fixed_positions_dict, omit_AA_dict, tied_positions_dict, pssm_dict)
            if not args.use_sc:
                X_m = X_m * 0
            if not args.use_DNA_RNA:
                Y_m = Y_m * 0
            if not args.use_ligand:
                Z_m = Z_m * 0
            if args.mask_hydrogen:
                mask_hydrogen = ~(Z_t == 40)  #1 for not hydrogen, 0 for hydrogen
                Z_m = Z_m*mask_hydrogen
            pssm_log_odds_mask = (pssm_log_odds_all > args.pssm_threshold).float() #1.0 for true, 0.0 for false
            name_ = batch_clones[0]['name']
            if args.score_only:
                structure_sequence_score_file = base_folder + '/score_only/' + batch_clones[0]['name']
                native_score_list = []
                score_log_prob_list = []
                for j in range(NUM_BATCHES):
                    randn_1 = torch.randn(chain_M.shape, device=X.device)
                    log_probs = model(X, X_m, Y, Y_m, Z, Z_m, Z_t, randn_1, S, chain_M*chain_M_pos, chain_encoding_all, residue_idx, mask)
                    mask_for_loss = mask*chain_M*chain_M_pos
                    scores = _scores(S, log_probs, mask_for_loss)
                    native_score = scores.cpu().data.numpy()
                    native_score_list.append(native_score)
                    score_log_prob_list.append(log_probs.cpu().data.numpy())
                native_score = np.concatenate(native_score_list, 0)
                score_log_prob = np.concatenate(score_log_prob_list, 0)
                ns_mean = native_score.mean()
                ns_mean_print = np.format_float_positional(np.float32(ns_mean), unique=False, precision=4)
                ns_std = native_score.std()
                ns_std_print = np.format_float_positional(np.float32(ns_std), unique=False, precision=4)
                ns_sample_size = native_score.shape[0]
                np.save(structure_sequence_score_file, native_score)
                dna_mask = Y_m.sum().long()
                if dna_mask:
                    np.savez(structure_sequence_score_file, score=native_score, log_probs=score_log_prob, S=S[0,].cpu().data.numpy(), mask=mask[0,].cpu().data.numpy(), X=X[0,].cpu().data.numpy(), X_m=X_m[0,].cpu().data.numpy(), Z=Z[0,].cpu().data.numpy(), Z_m=Z_m[0,].cpu().data.numpy(), Y=Y[0,].cpu().data.numpy(), Y_m=Y_m[0,].cpu().data.numpy())
                else:
                    np.savez(structure_sequence_score_file, score=native_score, log_probs=score_log_prob, S=S[0,].cpu().data.numpy(), mask=mask[0,].cpu().data.numpy(), X=X[0,].cpu().data.numpy(), X_m=X_m[0,].cpu().data.numpy(), Z=Z[0,].cpu().data.numpy(), Z_m=Z_m[0,].cpu().data.numpy())
                print(f'Score for {name_}, mean: {ns_mean_print}, std: {ns_std_print}, sample size: {ns_sample_size}')
            else:
                randn_1 = torch.randn(chain_M.shape, device=X.device)
                log_probs = model(X, X_m, Y, Y_m, Z, Z_m, Z_t, randn_1, S, chain_M*chain_M_pos, chain_encoding_all, residue_idx, mask)
                mask_for_loss = mask*chain_M*chain_M_pos
                scores = _scores(S, log_probs, mask_for_loss)
                native_score = scores.cpu().data.numpy()
                # Generate some sequences
                ali_file = base_folder + '/seqs/' + batch_clones[0]['name'] + '.fa'
                score_file = base_folder + '/scores/' + batch_clones[0]['name'] + '.npy'
                probs_file = base_folder + '/probs/' + batch_clones[0]['name'] + '.npz'
                print(f'Generating sequences for: {name_}')
                t0 = time.time()
                with open(ali_file, 'w') as f:
                    for temp in temperatures:
                        for j in range(NUM_BATCHES):
                            randn_2 = torch.randn(chain_M.shape, device=X.device)
                            if tied_positions_dict == None:
                                sample_dict = model.sample(X, X_m, Y, Y_m, Z, Z_m, Z_t, randn_2, S, chain_M, chain_encoding_all, residue_idx, mask=mask, temperature=temp, omit_AAs_np=omit_AAs_np, bias_AAs_np=bias_AAs_np, chain_M_pos=chain_M_pos, omit_AA_mask=omit_AA_mask, pssm_coef=pssm_coef, pssm_bias=pssm_bias, pssm_multi=args.pssm_multi, pssm_log_odds_flag=bool(args.pssm_log_odds_flag), pssm_log_odds_mask=pssm_log_odds_mask, pssm_bias_flag=bool(args.pssm_bias_flag))
                                S_sample = sample_dict["S"] 
                            else:
                                sample_dict = model.tied_sample(X, X_m, Y, Y_m, Z, Z_m, Z_t, randn_2, S, chain_M, chain_encoding_all, residue_idx, mask=mask, temperature=temp, omit_AAs_np=omit_AAs_np, bias_AAs_np=bias_AAs_np, chain_M_pos=chain_M_pos, omit_AA_mask=omit_AA_mask, pssm_coef=pssm_coef, pssm_bias=pssm_bias, pssm_multi=args.pssm_multi, pssm_log_odds_flag=bool(args.pssm_log_odds_flag), pssm_log_odds_mask=pssm_log_odds_mask, pssm_bias_flag=bool(args.pssm_bias_flag), tied_pos=tied_pos_list_of_lists_list[0], tied_beta=tied_beta)
                            # Compute scores
                                S_sample = sample_dict["S"]
                            log_probs = model(X, X_m, Y, Y_m, Z, Z_m, Z_t, randn_2, S_sample, chain_M*chain_M_pos, chain_encoding_all, residue_idx, mask)
                            mask_for_loss = mask*chain_M*chain_M_pos
                            scores = _scores(S_sample, log_probs, mask_for_loss)
                            scores = scores.cpu().data.numpy()
                            all_probs_list.append(sample_dict["probs"].cpu().data.numpy())
                            all_log_probs_list.append(log_probs.cpu().data.numpy())
                            S_sample_list.append(S_sample.cpu().data.numpy())
                            for b_ix in range(BATCH_COPIES):
                                masked_chain_length_list = masked_chain_length_list_list[b_ix]
                                masked_list = masked_list_list[b_ix]
                                seq_recovery_rate = torch.sum(torch.sum(torch.nn.functional.one_hot(S[b_ix], 21)*torch.nn.functional.one_hot(S_sample[b_ix], 21),axis=-1)*mask_for_loss[b_ix])/torch.sum(mask_for_loss[b_ix])
                                seq = _S_to_seq(S_sample[b_ix], chain_M[b_ix])
                                score = scores[b_ix]
                                score_list.append(score)
                                native_seq = _S_to_seq(S[b_ix], chain_M[b_ix])
                                if b_ix == 0 and j==0 and temp==temperatures[0]:
                                    start = 0
                                    end = 0
                                    list_of_AAs = []
                                    for mask_l in masked_chain_length_list:
                                        end += mask_l
                                        list_of_AAs.append(native_seq[start:end])
                                        start = end
                                    native_seq = "".join(list(np.array(list_of_AAs)[np.argsort(masked_list)]))
                                    l0 = 0
                                    for mc_length in list(np.array(masked_chain_length_list)[np.argsort(masked_list)])[:-1]:
                                        l0 += mc_length
                                        native_seq = native_seq[:l0] + '/' + native_seq[l0:]
                                        l0 += 1
                                    sorted_masked_chain_letters = np.argsort(masked_list_list[0])
                                    print_masked_chains = [masked_list_list[0][i] for i in sorted_masked_chain_letters]
                                    sorted_visible_chain_letters = np.argsort(visible_list_list[0])
                                    print_visible_chains = [visible_list_list[0][i] for i in sorted_visible_chain_letters]
                                    native_score_print = np.format_float_positional(np.float32(native_score.mean()), unique=False, precision=4)
                                    f.write('>{}, score={}, fixed_chains={}, designed_chains={}\n{}\n'.format(name_, native_score_print, print_visible_chains, print_masked_chains, native_seq)) #write the native sequence
                                start = 0
                                end = 0
                                list_of_AAs = []
                                for mask_l in masked_chain_length_list:
                                    end += mask_l
                                    list_of_AAs.append(seq[start:end])
                                    start = end
    
                                seq = "".join(list(np.array(list_of_AAs)[np.argsort(masked_list)]))
                                l0 = 0
                                for mc_length in list(np.array(masked_chain_length_list)[np.argsort(masked_list)])[:-1]:
                                    l0 += mc_length
                                    seq = seq[:l0] + '/' + seq[l0:]
                                    l0 += 1
                                score_print = np.format_float_positional(np.float32(score), unique=False, precision=4)
                                seq_rec_print = np.format_float_positional(np.float32(seq_recovery_rate.detach().cpu().numpy()), unique=False, precision=4)
                                f.write('>T={}, sample={}, score={}, seq_recovery={}\n{}\n'.format(temp,b_ix,score_print,seq_rec_print,seq)) #write generated sequence
                if args.save_score:
                    np.save(score_file, np.array(score_list, np.float32))
                if args.save_probs:
                    all_probs_concat = np.concatenate(all_probs_list)
                    all_log_probs_concat = np.concatenate(all_log_probs_list)
                    S_sample_concat = np.concatenate(S_sample_list)
                    np.savez(probs_file, probs=np.array(all_probs_concat, np.float32), log_probs=np.array(all_log_probs_concat, np.float32), S=np.array(S_sample_concat, np.int32), mask=mask_for_loss.cpu().data.numpy(), chain_order=chain_list_list)
                t1 = time.time()
                dt = round(float(t1-t0), 4)
                num_seqs = len(temperatures)*NUM_BATCHES*BATCH_COPIES
                total_length = X.shape[1]
                print(f'{num_seqs} sequences of length {total_length} generated in {dt} seconds')
   
if __name__ == "__main__":
    argparser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    
    file_path = os.path.realpath(__file__)
    k = file_path.rfind("/")
    #model_weigths_path = file_path[:k] + '/model_weights/lnet_plus4/epoch870_step231040.pt'
    model_weigths_path = file_path[:k] + '/model_weights/lnet_plus10/epoch2000_step219824.pt'
    argparser.add_argument("--checkpoint_path", type=str, default=model_weigths_path, help="Path to the model checkpoint")
    argparser.add_argument("--hidden_dim", type=int, default=128, help="Hidden dimension for the model")
    argparser.add_argument("--num_layers", type=int, default=3, help="Number of layers for the model")
    argparser.add_argument("--num_connections", type=int, default=48, help="Default 48")
 
    argparser.add_argument("--save_score", type=int, default=0, help="0 for False, 1 for True; save score=-log_prob to npy files")
    argparser.add_argument("--save_probs", type=int, default=0, help="0 for False, 1 for True; save MPNN predicted probabilites per position")

    argparser.add_argument("--score_only", type=int, default=0, help="0 for False, 1 for True; score input backbone-sequence pairs")

    argparser.add_argument("--use_sc", type=int, default=1, help="0 for False, 1 for True; use side chain context for fixed residues")
    argparser.add_argument("--use_DNA_RNA", type=int, default=1, help="0 for False, 1 for True; use RNA/DNA context")
    argparser.add_argument("--use_ligand", type=int, default=1, help="0 for False, 1 for True; use ligand context")

    argparser.add_argument("--mask_hydrogen", type=int, default=1, help="0 for False, 1 for True")
 
    argparser.add_argument("--backbone_noise", type=float, default=0.00, help="Standard deviation of Gaussian noise to add to backbone atoms")
    argparser.add_argument("--num_seq_per_target", type=int, default=1, help="Number of sequences to generate per target")
    argparser.add_argument("--batch_size", type=int, default=1, help="Batch size; can set higher for titan, quadro GPUs, reduce this if running out of GPU memory")
    argparser.add_argument("--max_length", type=int, default=20000, help="Max sequence length")
    argparser.add_argument("--sampling_temp", type=str, default="0.1", help="A string of temperatures, 0.2 0.25 0.5. Sampling temperature for amino acids, T=0.0 means taking argmax, T>>1.0 means sample randomly. Suggested values 0.1, 0.15, 0.2, 0.25, 0.3. Higher values will lead to more diversity.")
    
    argparser.add_argument("--out_folder", type=str, help="Path to a folder to output sequences, e.g. /home/out/")
    argparser.add_argument("--pdb_path", type=str, default='', help="Path to a single PDB to be designed")
    argparser.add_argument("--ligand_params_path", type=str, default='', help="Path to a params file for the single PDB")
    argparser.add_argument("--pdb_path_chains", type=str, default='', help="Define which chains need to be designed for a single PDB ")
    argparser.add_argument("--jsonl_path", type=str, help="Path to a folder with parsed pdb into jsonl")
    argparser.add_argument("--chain_id_jsonl",type=str, default='', help="Path to a dictionary specifying which chains need to be designed and which ones are fixed, if not specied all chains will be designed.")
    argparser.add_argument("--fixed_positions_jsonl", type=str, default='', help="Path to a dictionary with fixed positions")
    argparser.add_argument("--omit_AAs", type=list, default='X', help="Specify which amino acids should be omitted in the generated sequence, e.g. 'AC' would omit alanine and cystine.")
    argparser.add_argument("--bias_AA_jsonl", type=str, default='', help="Path to a dictionary which specifies AA composion bias if neededi, e.g. {A: -1.1, F: 0.7} would make A less likely and F more likely.")
    
    argparser.add_argument("--omit_AA_jsonl", type=str, default='', help="Path to a dictionary which specifies which amino acids need to be omited from design at specific chain indices")
    argparser.add_argument("--pssm_jsonl", type=str, default='', help="Path to a dictionary with pssm")
    argparser.add_argument("--pssm_multi", type=float, default=0.0, help="A value between [0.0, 1.0], 0.0 means do not use pssm, 1.0 ignore MPNN predictions")
    argparser.add_argument("--pssm_threshold", type=float, default=0.0, help="A value between -inf + inf to restric per position AAs")
    argparser.add_argument("--pssm_log_odds_flag", type=int, default=0, help="0 for False, 1 for True")
    argparser.add_argument("--pssm_bias_flag", type=int, default=0, help="0 for False, 1 for True")
    
    argparser.add_argument("--tied_positions_jsonl", type=str, default='', help="Path to a dictionary with tied positions")
    
    args = argparser.parse_args()    
    main(args)   
