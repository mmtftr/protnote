from itertools import product
import umap
from sklearn.preprocessing import StandardScaler,QuantileTransformer
from src.utils.data import generate_vocabularies
import os
import matplotlib.pyplot as plt
import seaborn as sns
import torch
import obonet
import argparse
from tqdm import tqdm

def save_fig(name):
    plt.savefig(f'{name}.pdf', format='pdf', dpi=1200,bbox_inches='tight')

if __name__=='__main__':

    parser = argparse.ArgumentParser(description="Run UMAP plot hparam search")
    parser.add_argument("--n-neighbors-vals", nargs="+", type=int, required=False, default = [50,200], help="n neighbors values to try")
    parser.add_argument("--min-dist-vals", nargs="+", type=float, required=False, default = [0.5,0.3], help="min dist values to try")
    parser.add_argument("--paired-hparams", action='store_true',default=False)
    parser.add_argument("--num-seqs", type=int, required=False,default=100, help="Number of sequences to consider")
    parser.add_argument("--output_dir", type=str, required=False,default='outputs/results/umap_figs/', help="path to save results")
    
    args = parser.parse_args()
    
    plt.rcParams['font.size'] = 14

    if not os.path.exists(args.output_dir):
        os.mkdir(args.output_dir)

    print('reading data...')
    graph = obonet.read_obo('data/annotations/go_jul_2024.obo')
    embeddings1 = torch.load('outputs/results/test_1_embeddings_TEST_TOP_LABELS_DATA_PATH_normal_test_label_aug_v4/batches_0_99.pt',
                map_location='cpu')
    joint_embedding_dim = embeddings1['joint_embeddings'].shape[-1]


    # embeddings2 = torch.load('../outputs/results/test_1_embeddings_TEST_TOP_LABELS_DATA_PATH_normal_test_label_aug_v4/batches_100_199.pt',
    #                map_location='cpu')

    num_labels = embeddings1['labels'].shape[-1]
    vocab = generate_vocabularies('data/swissprot/proteinfer_splits/random/test_top_labels_GO.fasta')['label_vocab']
    graph = obonet.read_obo('data/annotations/go_jul_2019.obo')
    vocab_parents = [(graph.nodes[go_term]["namespace"] if  go_term in graph.nodes else 'missing') for go_term in vocab]

    print('pre processing...')
    X = embeddings1['output_layer_embeddings'][:num_labels*args.num_seqs,:]
    sc = StandardScaler()
    X_s = sc.fit_transform(X)


    X_labels = embeddings1['joint_embeddings'][:num_labels,joint_embedding_dim//2:]
    sc_labels = StandardScaler()
    X_labels_s = sc_labels.fit_transform(X_labels)

    hparams = [args.n_neighbors_vals,args.min_dist_vals]
    num_combinations = 1

    if args.paired_hparams:
        assert len(hparams[0]) == len(hparams[1]),'hparams must be same lenght with paired_hparams = true'
        num_combinations = len(args.n_neighbors_vals)
        combos = list(zip(*hparams))
    else:
        for hparam in hparams:
            num_combinations *= len(hparam)
        combos = product(*hparams)
    print(f"Testing {num_combinations} hparam combinations")


    hue = vocab_parents*(args.num_seqs)
    mask = [i !='missing' for i in hue]
    hue_masked =[hue_val for hue_val,mask_val in zip(hue,mask) if mask_val]
    match_binary_mask = embeddings1['labels'][:args.num_seqs,:].flatten()

    palette = sns.color_palette("tab10")
    
    print('running umap plots...')
    for n_neighbors,min_dist in tqdm(combos,total= num_combinations):
        title = f'match vs unmatch n_neighbors={n_neighbors}, min_dist={min_dist}'
        X_r = umap.UMAP(
                n_neighbors=n_neighbors,
                min_dist=min_dist).fit(X_s).embedding_

        fig = plt.figure(figsize=(7,7))
        
        # output layer showing separation between matching and un-matching protein-function pairs
        palette_ = palette[7:8] + palette[6:7]
        sns.scatterplot(x=X_r[:,0],y=X_r[:,1],marker='.',s = 2, hue=match_binary_mask,edgecolor=None,palette=palette_)
        plt.legend(markerscale=10,title="Protein-Function Label", bbox_to_anchor=(0.5, -0.2), loc='upper center')
        sns.despine()
        plt.title(title)
        save_fig(os.path.join(args.output_dir,title))
        plt.show()


        # Output layer colored by GO Top hierarchy
        fig = plt.figure(figsize=(7,7))
        title = f'top hierarchy n_neighbors={n_neighbors}, min_dist={min_dist}'
        palette_ = palette[4:5] + palette[8:10]
        match_binary_mask = match_binary_mask.astype(bool) & mask
        X_r = umap.UMAP(
            n_neighbors=n_neighbors,
            min_dist=min_dist).fit(X_s[match_binary_mask]).embedding_
        sns.scatterplot(x=X_r[:,0],
                        y=X_r[:,1],
                        marker='.',
                        hue=[hue_val for hue_val,mask_val,binary_mask_val in zip(hue,mask,match_binary_mask) if mask_val & binary_mask_val],
                        s=15,
                        edgecolor=None,
                        palette=palette_)
        plt.legend(markerscale=1,title="Ontology", bbox_to_anchor=(0.5, -0.2), loc='upper center')
        sns.despine()
        plt.title(title)
        save_fig(os.path.join(args.output_dir,title))
        plt.show()


        # hue = vocab_parents
        # mask = [i !='missing' for i in hue]
        # hue_masked =[hue_val for hue_val,mask_val in zip(hue,mask) if mask_val]

        # palette = sns.color_palette("tab10")
        # palette_ = palette[4:5] + palette[8:10]


        # X_r = umap.UMAP(
        #         n_neighbors=n_neighbors,
        #         min_dist=min_dist).fit(X_labels_s).embedding_


        # fig = plt.figure(figsize=(7,7))
        # title = f'top hierarchy embeddings: n_neighbors={n_neighbors}, min_dist={min_dist}'
        # sns.scatterplot(x=X_r[mask][:,0],y=X_r[mask][:,1],marker='.',hue=hue_masked,s=20,edgecolor=None,palette=palette_)
        # plt.legend(markerscale=1,title="Ontology", bbox_to_anchor=(0.5, -0.2), loc='upper center')
        # sns.despine()
        # plt.title(title)
        # save_fig(os.path.join(args.output_dir,title))
        # plt.show()
