from functools import partial
import numpy as np
from typing import List, Tuple, Dict, Union, Optional, Callable, Any
from tqdm import tqdm
import torch
from easy_transformer import EasyTransformer
from easy_transformer.experiments import get_act_hook
from ioi_dataset import (
    IOIDataset,
)
import warnings
import matplotlib.pyplot as plt
import networkx as nx
from collections import OrderedDict


def get_hook_tuple(layer, head_idx):
    if head_idx is None:
        return (f"blocks.{layer}.hook_mlp_out", None)
    else:
        return (f"blocks.{layer}.attn.hook_result", head_idx)


def patch_all(z, source_act, hook):
    z[:] = source_act  # make sure to slice! Otherwise objects get copied around
    return z


def patch_positions(z, source_act, hook, positions):
    if positions is None:  # same as patch_all
        raise NotImplementedError(
            "haven't implemented not specifying positions to patch"
        )
        # return source_act
    else:
        batch = z.shape[0]
        for pos in positions:
            z[torch.arange(batch), pos] = source_act[torch.arange(batch), pos]
        return z


def path_patching(
    model: EasyTransformer,
    orig_data,
    new_data,
    senders: List[Tuple],
    receiver_hooks: List[Tuple],
    max_layer: Union[int, None] = None,
    position: int = 0,
    return_hooks: bool = False,
    freeze_mlps: bool = True,
    orig_cache=None,
    new_cache=None,
    prepend_bos=False,  # we did IOI with prepend_bos = False, but in general we think True is less sketchy. Currently EasyTransformer sometimes does one and sometimes does the other : (
):
    """mlps are by default considered as just another component and so are
    by default frozen when collecting acts on receivers.
    orig_data: string, torch.Tensor, or list of strings - any format that can be passed to the model directly
    new_data: same as orig_data
    senders: list of tuples (layer, head) for attention heads and (layer, None) for mlps
    receiver_hooks: list of tuples (hook_name, head) for attn heads and (hook_name, None) for mlps
    max_layer: layers beyond max_layer are not frozen when collecting receiver activations
    positions: default None and patch at all positions, or a tensor specifying the positions at which to patch

    NOTE: This relies on a change to the cache_some() function in EasyTransformer/hook_points.py.
    """
    if max_layer is None:
        max_layer = model.cfg.n_layers
    assert max_layer <= model.cfg.n_layers

    if orig_cache is None:
        # save activations from orig
        orig_cache = {}
        model.reset_hooks()
        model.cache_all(orig_cache)
        _ = model(orig_data, prepend_bos=False)

    # process senders
    sender_hooks = []
    for layer, head in senders:
        if head is not None:  # (layer, head) for attention heads
            sender_hooks.append((f"blocks.{layer}.attn.hook_result", head))
        else:  # (layer, None) for mlps
            sender_hooks.append((f"blocks.{layer}.hook_mlp_out", None))
    sender_hook_names = [x[0] for x in sender_hooks]

    if new_cache is None:
        # save activations from new for senders
        model.reset_hooks()
        new_cache = {}
        model.cache_some(new_cache, lambda x: x in sender_hook_names)
        _ = model(new_data, prepend_bos=False)
    else:
        assert all(
            [x in new_cache for x in sender_hook_names]
        ), f"Difference between new_cache and senders: {set(sender_hook_names) - set(new_cache.keys())}"

    # set up receiver cache
    model.reset_hooks()
    receiver_hook_names = [x[0] for x in receiver_hooks]
    receiver_cache = {}
    model.cache_some(receiver_cache, lambda x: x in receiver_hook_names)

    # configure hooks for freezing activations
    for layer in range(max_layer):
        # heads
        for head in range(model.cfg.n_heads):
            # if (layer, head) in senders:
            #     continue
            for hook_template in [
                "blocks.{}.attn.hook_q",
                "blocks.{}.attn.hook_k",
                "blocks.{}.attn.hook_v",
            ]:
                hook_name = hook_template.format(layer)
                hook = get_act_hook(
                    patch_all,
                    alt_act=orig_cache[hook_name],
                    idx=head,
                    dim=2,
                )
                model.add_hook(hook_name, hook)
        # mlp
        if freeze_mlps:
            hook_name = f"blocks.{layer}.hook_mlp_out"
            hook = get_act_hook(
                patch_all,
                alt_act=orig_cache[hook_name],
                idx=None,
                dim=None,
            )
            model.add_hook(hook_name, hook)

    # for senders, add new hook to patching in new acts
    for hook_name, head in sender_hooks:
        # assert not torch.allclose(orig_cache[hook_name], new_cache[hook_name]), (hook_name, head)
        hook = get_act_hook(
            partial(patch_positions, positions=[position]),
            alt_act=new_cache[hook_name],
            idx=head,
            dim=2 if head is not None else None,
        )
        model.add_hook(hook_name, hook)

    # forward pass on orig, where patch in new acts for senders and orig acts for the rest
    # and save activations on receivers
    _ = model(orig_data, prepend_bos=False)
    model.reset_hooks()

    # add hooks for final forward pass on orig, where we patch in hybrid acts for receivers
    hooks = []
    for hook_name, head in receiver_hooks:
        # assert not torch.allclose(orig_cache[hook_name], receiver_cache[hook_name])
        hook = get_act_hook(
            partial(patch_positions, positions=[position]),
            alt_act=receiver_cache[hook_name],
            idx=head,
            dim=2 if head is not None else None,
        )
        hooks.append((hook_name, hook))

    if return_hooks:
        return hooks
    else:
        for hook_name, hook in hooks:
            model.add_hook(hook_name, hook)
        return model


def path_patching_up_to(
    model: EasyTransformer,
    layer: int,
    metric,
    dataset,
    orig_data,
    new_data,
    receiver_hooks,
    position,
    orig_cache=None,
    new_cache=None,
):
    model.reset_hooks()
    attn_results = np.zeros((layer, model.cfg.n_heads))
    mlp_results = np.zeros((layer, 1))
    for l in tqdm(range(layer)):
        for h in range(model.cfg.n_heads):
            model = path_patching(
                model,
                orig_data=orig_data,
                new_data=new_data,
                senders=[(l, h)],
                receiver_hooks=receiver_hooks,
                max_layer=model.cfg.n_layers,
                position=position,
                orig_cache=orig_cache,
                new_cache=new_cache,
            )
            attn_results[l, h] = metric(model, dataset)
            model.reset_hooks()
        # mlp
        model = path_patching(
            model,
            orig_data=orig_data,
            new_data=new_data,
            senders=[(l, None)],
            receiver_hooks=receiver_hooks,
            max_layer=model.cfg.n_layers,
            position=position,
            orig_cache=orig_cache,
            new_cache=new_cache,
        )
        mlp_results[l] = metric(model, dataset)
        model.reset_hooks()
    return attn_results, mlp_results


def logit_diff_io_s(model: EasyTransformer, dataset: IOIDataset):
    N = dataset.N
    io_logits = model(dataset.toks.long())[
        torch.arange(N), dataset.word_idx["end"], dataset.io_tokenIDs
    ]
    s_logits = model(dataset.toks.long())[
        torch.arange(N), dataset.word_idx["end"], dataset.s_tokenIDs
    ]
    return (io_logits - s_logits).mean().item()


class Node:
    def __init__(self, layer: int, head: int, position: str):
        self.layer = layer
        self.head = head
        assert isinstance(
            position, str
        ), f"Position must be a string, not {type(position)}"
        self.position = position
        self.children = []
        self.parents = []

    def __repr__(self):
        return f"Node({self.layer}, {self.head}, {self.position})"

    def repr_long(self):
        return f"Node({self.layer}, {self.head}, {self.position}) with children {[child.__repr__() for child in self.children]}"


class HypothesisTree:
    def __init__(
        self,
        model: EasyTransformer,
        metric: Callable,
        dataset,
        orig_data,
        new_data,
        threshold: int,
        possible_positions: OrderedDict,
        use_caching: bool = True,
    ):
        self.model = model
        self.possible_positions = possible_positions
        self.node_stack = OrderedDict()
        self.populate_node_stack()
        self.current_node = self.node_stack[
            next(reversed(self.node_stack))
        ]  # last element
        self.root_node = self.current_node
        self.metric = metric
        self.dataset = dataset
        self.orig_data = orig_data
        self.new_data = new_data
        self.threshold = threshold
        self.default_metric = self.metric(model, dataset)
        self.orig_cache = None
        self.new_cache = None
        if use_caching:
            self.get_caches()
        self.important_nodes = []

    def populate_node_stack(self):
        for layer in range(self.model.cfg.n_layers):
            for head in list(range(self.model.cfg.n_heads)) + [
                None
            ]:  # includes None for mlp
                for pos in self.possible_positions:
                    node = Node(layer, head, pos)
                    self.node_stack[(layer, head, pos)] = node
        layer = self.model.cfg.n_layers
        pos = next(
            reversed(self.possible_positions)
        )  # assume the last position specified is the one that we care about in the residual stream
        resid_post = Node(layer, None, pos)
        self.node_stack[
            (layer, None, pos)
        ] = resid_post  # this represents blocks.{last}.hook_resid_post

    def get_caches(self):
        if "orig_cache" in self.__dict__.keys():
            warnings.warn("Caches already exist, overwriting")

        # save activations from orig
        self.orig_cache = {}
        self.model.reset_hooks()
        self.model.cache_all(self.orig_cache)
        _ = self.model(self.orig_data, prepend_bos=False)

        # save activations from new for senders
        self.new_cache = {}
        self.model.reset_hooks()
        self.model.cache_all(self.new_cache)
        _ = self.model(self.new_data, prepend_bos=False)

    def eval(
        self,
        threshold: Union[float, None] = None,
        verbose: bool = False,
        show_graphics: bool = True,
        auto_threshold: bool = False,
    ):
        """Process current_node, then move to next current_node"""

        if threshold is None:
            threshold = self.threshold

        _, node = self.node_stack.popitem()
        self.important_nodes.append(node)
        print("Currently evaluating", node)

        current_node_position = node.position
        for pos in self.possible_positions:
            if (
                current_node_position != pos and node.head is None
            ):  # MLPs and the end state of the residual stream only care about the last position
                continue

            receiver_hooks = []
            if node.layer == self.model.cfg.n_layers:
                receiver_hooks.append((f"blocks.{node.layer-1}.hook_resid_post", None))
            elif node.head is None:
                receiver_hooks.append((f"blocks.{node.layer}.hook_mlp_out", None))
            else:
                receiver_hooks.append((f"blocks.{node.layer}.attn.hook_v", node.head))
                receiver_hooks.append((f"blocks.{node.layer}.attn.hook_k", node.head))
                if pos == current_node_position:
                    receiver_hooks.append(
                        (f"blocks.{node.layer}.attn.hook_q", node.head)
                    )  # similar story to above, only care about the last position

            for receiver_hook in receiver_hooks:
                if verbose:
                    print(f"Working on pos {pos}, receiver hook {receiver_hook}")
                attn_results, mlp_results = path_patching_up_to(
                    model=self.model,
                    layer=node.layer,
                    metric=self.metric,
                    dataset=self.dataset,
                    orig_data=self.orig_data,
                    new_data=self.new_data,
                    receiver_hooks=[receiver_hook],
                    position=self.possible_positions[
                        pos
                    ],  # TODO TODO TODO I think we might need to have an "in position" (pos) as well as an "out position" (node.position)
                    orig_cache=self.orig_cache,
                    new_cache=self.new_cache,
                )

                # convert to percentage
                attn_results -= self.default_metric
                attn_results /= self.default_metric
                mlp_results -= self.default_metric
                mlp_results /= self.default_metric
                self.attn_results = attn_results
                self.mlp_results = mlp_results

                if show_graphics:
                    show_pp(
                        attn_results.T,
                        title=f"Attn results for {node} with receiver hook {receiver_hook}",
                        xlabel="Head",
                        ylabel="Layer",
                    )
                    show_pp(
                        mlp_results,
                        title=f"MLP results for {node} with receiver hook {receiver_hook}",
                        xlabel="Layer",
                        ylabel="",
                    )

                if auto_threshold:
                    threshold = max(3 * attn_results.std(), 3 * mlp_results.std(), 0.01)
                # process result and mark nodes above threshold as important
                for layer in range(attn_results.shape[0]):
                    for head in range(attn_results.shape[1]):
                        if abs(attn_results[layer, head]) > threshold:
                            print(
                                "Found important head:",
                                (layer, head),
                                "at position",
                                pos,
                            )
                            score = attn_results[layer, head]
                            comp_type = receiver_hook[0].split("_")[
                                -1
                            ]  # q, k, v, out, post
                            self.node_stack[(layer, head, pos)].children.append(
                                (node, score, comp_type)
                            )
                            node.parents.append(
                                (self.node_stack[(layer, head, pos)], score, comp_type)
                            )
                    if abs(mlp_results[layer]) > threshold:
                        print("Found important MLP: layer", layer, "position", pos)
                        score = mlp_results[layer, 0]
                        comp_type = receiver_hook[0].split("_")[
                            -1
                        ]  # q, k, v, out, post
                        self.node_stack[(layer, None, pos)].children.append(
                            (node, score, comp_type)
                        )
                        node.parents.append(
                            (self.node_stack[(layer, None, pos)], score, comp_type)
                        )

        # update self.current_node
        while (
            len(self.node_stack) > 0
            and len(self.node_stack[next(reversed(self.node_stack))].children) == 0
        ):
            self.node_stack.popitem()
        if len(self.node_stack) > 0:
            self.current_node = self.node_stack[next(reversed(self.node_stack))]
        else:
            self.current_node = None

    def show(self, save=False):
        edge_color_list = []
        color_dict = {
            "q": "black",
            "k": "blue",
            "v": "green",
            "out": "red",
            "post": "red",
        }
        current_node = h.root_node
        G = nx.DiGraph()

        def dfs(node):
            G.add_nodes_from([(node, {"layer": node.layer})])
            for child_node, child_score, child_type in node.parents:
                G.add_edges_from(
                    [(node, child_node, {"weight": round(child_score, 3)})]
                )
                edge_color_list.append(color_dict[child_type])
                dfs(child_node)

        dfs(current_node)
        pos = nx.multipartite_layout(G, subset_key="layer")
        # make plt figure fills screen
        fig = plt.figure(dpi=300, figsize=(24, 24))
        nx.draw(
            G,
            pos,
            node_size=8000,
            node_color="#b0a8a7",
            linewidths=2.0,
            edge_color=edge_color_list,
            width=1.5,
            arrowsize=12,
        )

        nx.draw_networkx_labels(G, pos, font_size=14)
        edge_labels = nx.get_edge_attributes(G, "weight")
        nx.draw_networkx_edge_labels(G, pos, edge_labels)

        if save:
            plt.savefig("ioi_circuit.png")
