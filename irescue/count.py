#!/usr/bin/env python

from collections import Counter
from itertools import permutations
import numpy as np
import networkx as nx
from irescue.misc import get_ranges, getlen, writerr, run_shell_cmd
from irescue.em import run_em
import gzip
import os

def connect_umis(x, y, threshold=1):
    """
    Check if UMI x connects to y.

    x, y : iterable
        (UMI, TE, count) : (str, set, int)
    """
    return (x[2] >= (2 * y[2]) - 1
            and x[1].intersection(y[1])
            and sum(1 for i, j in zip(x[0], y[0]) if i != j) <= threshold)

def pathfinder(graph, node, path=[], features=None):
    """
    Finds first valid path of UMIs with compatible equivalence class given
    a starting node. Can be used iteratively to find all possible paths.
    """
    if not features:
        features = graph.nodes[node]['ft']
    path += [node]
    for next_node in graph.successors(node):
        if (features.intersection(graph.nodes[next_node]['ft'])
            and next_node not in path):
            path = pathfinder(graph, next_node, path, features)
    return path

def index_features(features_file):
    idx = {}
    with gzip.open(features_file, 'rb') as f:
        for i, line in enumerate(f, start=1):
            ft = line.strip().split(b'\t')[0]
            idx[ft] = i
    return idx

def parse_maps(maps_file, feature_index):
    """
    maps_file : str
        Content: "CB UMI FEATs count"
    out : bytes, list
        CB,
        [(UMI <str>, {FT <int>, ...} <set>, count <int>) <tuple>, ...]
    """
    with gzip.open(maps_file, 'rb') as f:
        eqcl = []
        it = f.readline().strip().split(b'\t')[0]
        f.seek(0)
        for line in f:
            cb, umi, feat, count = line.strip().split(b'\t')
            count = int(count)
            feat = {feature_index[ft] for ft in feat.split(b',')}
            if cb == it:
                eqcl.append((umi, feat, count))
            else:
                yield it, eqcl
                it = cb
                eqcl = [(umi, feat, count)]
        yield it, eqcl

def compute_cell_counts(equivalence_classes, features_index, dumpEC):
    """
    Calculate TE counts of a single cell, given a list of equivalence classes.

    Parameters
    ----------
    equivalence_classes : list
        (UMI_sequence, {TE_index}, read_count) : (str, set, int)
        Tuples containing UMI-TEs equivalence class infos.

    Returns
    -------
    out : dict
        feature <int>: count <float> dictionary.
    """
    # initialize TE counts and dedup log
    counts = Counter()
    dump = None
    number_of_features = len(features_index)
    # build cell-wide UMI deduplication graph
    graph = nx.DiGraph()
    # add nodes with annotated features
    graph.add_nodes_from(
        [(i, {'ft': j[1], 'c': j[2]}) for i, j in enumerate(equivalence_classes)]
    )
    # helper function to return connected equivalence classes indexes
    def conn(x, y):
        if connect_umis(x[1], y[1]):
            return x[0], y[0]
    # add edges
    for x, y in permutations(enumerate(equivalence_classes), 2):
        if connect_umis(x[1], y[1]):
            graph.add_edge(x[0], y[0])
    if dumpEC:
        # collect graph metadata in a dictionary
        dump = {i: equivalence_classes[i] for i in graph.nodes}
    # split cell-wide graph into subgraphs of connected nodes
    subgraphs = [graph.subgraph(x) for x in
                 nx.connected_components(graph.to_undirected())]
    # put aside networks that will be solved with EM
    em_array = []
    # solve UMI deduplication for each subgraph of connected nodes
    for subg in subgraphs:
        # find all parent nodes in graph
        parents = [x for x in subg if not list(subg.predecessors(x))]
        if not parents:
            # if no parents are found due to bidirected edges, take all nodes
            # and the union of all features (i.e. all nodes are parents).
            parents = list(subg.nodes)
            features = [list(set.union(*[subg.nodes[x]['ft'] for x in subg]))]
        else:
            # if parents node are found, features will be determined below.
            features = None
        # initialize dict of possible paths configurations, starting from
        # each parent node.
        paths = {x: [] for x in parents}
        # find paths starting from each parent node
        for parent in parents:
            # populate this list with nodes utilized in paths
            blacklist = []
            # find paths in list of nodes starting from parent
            path = []
            subg_copy = subg.copy()
            nodes = [parent] + [x for x in subg_copy if x != parent]
            for node in nodes:
                # make a copy of subgraph and remove nodes already used
                # in a path
                if node not in blacklist:
                    path = pathfinder(subg_copy, node, path=[], features=None)
                    for x in path:
                        blacklist.append(x)
                        subg_copy.remove_node(x)
                    paths[parent].append(path)
        # find the path configuration leading to the minimum number of
        # deduplicated UMIs -> list of lists of nodes
        path_config = [
            paths[k] for k, v in paths.items()
            if len(v) == min([len(x) for x in paths.values()])
        ][0]
        if not features:
            # take features from parent node of selected path configuration
            features = [list(subg.nodes[x[0]]['ft']) for x in path_config]
        else:
            # if features was already determined (i.e. no parent nodes),
            # multiplicate the feature's list by the number of paths
            # in path_config to avoid going out of list range
            features *= len(path_config)
        # assign UMI count to features
        for feats in features:
            if len(feats) == 1:
                counts[feats[0]] += 1.0
            elif len(feats) > 1:
                row = [1 if x in feats else 0
                       for x in range(1, number_of_features+1)]
                em_array.append(row)
            else:
                writerr(nx.to_dict_of_lists(subg))
                writerr([subg.nodes[x]['ft'] for x in subg.nodes])
                writerr([subg.nodes[x]['count'] for x in subg.nodes])
                writerr(path_config)
                writerr(path)
                writerr(features)
                writerr(feats)
                writerr("Error: no common features detected in subgraph's"
                        " path.", error=True)
        # add EC log to dump
        if dumpEC:
            for i, path_ in enumerate(path_config):
                # add empty fields to parent node
                parent_ = path_[0]
                path_.pop(0)
                dump[parent_] += (b'', b'')
                # if child nodes are present, add parent node informations
                for x in path_:
                    # add parent's UMI sequence and dedup features
                    dump[x] += (dump[parent_][0], features[i])
    if em_array:
        # optimize the assignment of UMI from multimapping reads
        em_array = np.array(em_array)
        # save an array with features > 0, as in em_array order
        tokeep = np.argwhere(np.any(em_array[..., :] > 0, axis=0))[:,0] + 1
        # remove unmapped features from em_array
        todel = np.argwhere(np.all(em_array[..., :] == 0, axis=0))
        em_array = np.delete(em_array, todel, axis=1)
        # run EM
        em_counts = run_em(em_array, cycles=100)
        em_counts = [x*em_array.shape[0] for x in em_counts]
        for i, c in zip(tokeep, em_counts):
            if c > 0:
                counts[i] += c
    return dict(counts), dump

def split_barcodes(barcodes_file, n):
    """
    barcode_file : iterable
    n : int
    ----------
    out : int, dict
    """
    nBarcodes = getlen(barcodes_file)
    with gzip.open(barcodes_file, 'rb') as f:
        for i, chunk in enumerate(get_ranges(nBarcodes, n)):
            yield i, {next(f).strip(): x+1 for x in chunk}

def run_count(maps_file, features_index, tmpdir, dumpEC, verbose,
              barcodes_set):
    # NB: keep args order consistent with main.countFun
    taskn, barcodes = barcodes_set
    matrix_file = os.path.join(tmpdir, f'{taskn}_matrix.mtx.gz')
    dump_file = os.path.join(tmpdir, f'{taskn}_EqCdump.tsv.gz')
    with (gzip.open(matrix_file, 'wb') as f,
          gzip.open(dump_file, 'wb') if dumpEC
          else gzip.open(os.devnull) as df):
        for cellbarcode, cellmaps in parse_maps(maps_file, features_index):
            if cellbarcode not in barcodes:
                continue
            cellidx = barcodes[cellbarcode]
            writerr(f'[{taskn}] Run count for cell {cellidx} ({cellbarcode.decode()})',
                    send=verbose)
            cellcounts, dump = compute_cell_counts(
                equivalence_classes=cellmaps,
                features_index=features_index,
                dumpEC=dumpEC
            )
            writerr(f'[{taskn}] Write count for cell {cellidx} ({cellbarcode.decode()})',
                    send=verbose)
            # round counts to 3rd decimal point and write to matrix file
            # only if count is at least 0.001
            lines = [f'{feature} {cellidx} {round(count, 3)}\n'.encode()
                     for feature, count in cellcounts.items()
                     if count >= 0.001]
            f.writelines(lines)
            if dumpEC:
                writerr(f'[{taskn}] Write ECdump for cell {cellidx} ({cellbarcode.decode()})',
                        send=verbose)
                # reverse features index to get names back
                findex = dict(zip(features_index.values(),
                                  features_index.keys()))
                dumplines = [
                    b'\t'.join(
                        [str(cellidx).encode(),
                         cellbarcode,
                         str(i).encode(),
                         umi,
                         b','.join([findex[f] for f in feats]),
                         str(count).encode(),
                         pumi,
                         b','.join([findex[f] for f in pfeats])]
                    ) + b'\n'
                    for i, (umi, feats, count, pumi, pfeats) in dump.items()
                ]
                df.writelines(dumplines)
    return matrix_file, dump_file

def formatMM(matrix_files, feature_index, barcodes_chunks, outdir):
    if type(matrix_files) is str:
        matrix_files = [matrix_files]
    matrix_out = os.path.join(outdir, 'matrix.mtx.gz')
    features_count = len(feature_index)
    barcodes_count = sum(len(x) for _, x in barcodes_chunks)
    mmsize = sum(getlen(f) for f in matrix_files)
    mmheader = b'%%MatrixMarket matrix coordinate real general\n'
    mmtotal = f'{features_count} {barcodes_count} {mmsize}\n'.encode()
    with gzip.GzipFile(matrix_out, 'wb', mtime=0) as mmout:
        mmout.write(mmheader)
        mmout.write(mmtotal)
    mtxstr = ' '.join(matrix_files)
    cmd = f'zcat {mtxstr} | LC_ALL=C sort -k2,2n -k1,1n | gzip >> {matrix_out}'
    run_shell_cmd(cmd)
    return(matrix_out)

def writeEC(ecdump_files, outdir):
    if type(ecdump_files) is str:
        ecdump_files = [ecdump_files]
    ecdump_out = os.path.join(outdir, 'ec_dump.tsv.gz')
    ecdumpstr = ' '.join(ecdump_files)
    header = '\t'.join([
        'BC_index',
        'Barcode',
        'UMI',
        'Features',
        'Read_count',
        'Dedup_UMI',
        'Dedup_feature'
    ]) + '\n'
    with gzip.GzipFile(ecdump_out, 'wb', mtime=0) as f:
        f.write(header.encode())
    cmd = f'zcat {ecdumpstr} | LC_ALL=C sort -k1,1n -k2 | gzip >> {ecdump_out}'
    run_shell_cmd(cmd)
    return ecdump_out