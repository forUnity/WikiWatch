from metric import Metric
from datahandler import DataHandler
from utils.memory_tracker import log_memory_snapshot

from collections import defaultdict

import pandas as pd
import datetime

from graph_tool import Graph, GraphView
from graph_tool.topology import shortest_distance

all_edits_query="""--sql
select timestamp, action, new_value as reference, entity_id, property_id, ref_property_id, value_id from reference_change
WHERE "action" = 'CREATE' and "change_target" = ''
UNION ALL
select timestamp, action, old_value as reference, entity_id, property_id, ref_property_id, value_id from reference_change
WHERE "action" = 'DELETE' and "change_target" = ''
ORDER BY timestamp ASC
"""

from metrics.reference_domain_counts import domain_from_reference, ReferenceCountPerDomain

#Future work:
# + instead of contracting, speed up by deleting refs. This trades off accuracy for speed.
# + runningly update the classification sets and only touch domains involved in changes per timestep.
# + discuss parameters further with wikidata
# + reintroduce currently deleted statistical tests but get better global guarantees by getting independency of disconnected components of the graph.

class ReferenceTrustworthiness(Metric):
    #this version is without any disused code like Pagerank, Elo, Stat tests, and other classification methods.
    #it contains further optimization methods, mainly by switching from networkx to graph-tool.
    metric_name = "reference_trustworthiness"
    time_delta_for_expressing_preference = datetime.timedelta(days=1)
    def __init__(
        self,
        datahandler : DataHandler,
        count_of_references_per_domain : ReferenceCountPerDomain,
        classification_method : str = "simple_with_path",
        top_largest_domains_to_write: int = 1000,
        low_mass_contraction_threshold: int = 2,
        min_graph_size_for_contraction: int = 5000,
        discard_bottom_edge_fraction: float | None = 0.05,
    ):
        self.datahandler = datahandler
        self.dependencies: list[Metric] = [ count_of_references_per_domain ]

        self.count_of_references_per_domain = count_of_references_per_domain

        self.reference_graph = Graph(directed=True)
        self.reference_graph.set_fast_edge_lookup(True)
        self.vertex_domain = self.reference_graph.new_vertex_property("string")
        self.edge_weight = self.reference_graph.new_edge_property("int")
        self.edge_accepted = self.reference_graph.new_edge_property("bool")
        self.reference_graph.vp["domain"] = self.vertex_domain
        self.reference_graph.ep["weight"] = self.edge_weight
        self.reference_graph.ep["accepted"] = self.edge_accepted
        self.domain_to_vertex: dict[str, object] = {}
        self.low_mass_contraction_threshold = low_mass_contraction_threshold
        self.min_graph_size_for_contraction = min_graph_size_for_contraction
        self.contracted_domain_name = "__LOW_MASS_CONTRACTED__"
        self.contracted_mass_cumulative = 0.0
        self.contracted_mass_last_step = 0.0

        self.last_creates_dict = dict()
        self.last_create_timestamp_dict = dict()

        #methods and parameters
        #edge_flow method
        # self.m_pref_graph_method = graph_creation_method
        self.m_pref_graph_method = "discard_edge_heuristic"
        self.heuristic_absolute_cuttoff = 3 #5 used up to run_id=1819577 
        self.heuristic_fractional_cutoff = 0.01 #0.0001 used up to run_id=1819577 #but is per edge, thus requiring another reference domain of sufficient size to be better.
        if (
            discard_bottom_edge_fraction is not None
            and not 0.0 <= discard_bottom_edge_fraction < 1.0
        ):
            raise ValueError("discard_bottom_edge_fraction must be in [0.0, 1.0).")
        # Optional extra filter for discard_edge_heuristic: drop the lowest x% by edge weight.
        self.heuristic_discard_bottom_edge_fraction = discard_bottom_edge_fraction
        self.heuristic_fraction_of_reverse_to_add = 0.8 #1.2 used up to run_id=1819577 (and on antonios run)
   
        #GOOD: abs margin + weight flow OR stat_test + dom_doming based on 0 or any degree? 

        self.m_classification_method = "weight_flow" # "weight_flow" #"simple_degree" # #"edge_flow"
        self.m_classification_method = "simple_with_path"
        self.m_classification_method = classification_method
        self.largest_domains_to_write_metric = top_largest_domains_to_write

        if self.m_classification_method == "weight_flow":
            self.m_pref_graph_method = "keep_all_edges"

        self.m_weight_flow_threshold = 0.7 #0.55 used up to run_id=1819577 # only classify as good / bad if outweight is at least this fraction of total weight (out + in)
        self.m_weigth_flow_skip_if_too_few_changes_wrt_total = 0.01 #0.05 used up to run_id=1819577 # skip classification if there are too few changes compared to total count for this domain (to avoid classifying based on very little data)

        self.m_elicitation_method = "session" # or "replacements"
        self.m_session_death_time = datetime.timedelta(days=2)#7 used up to run_id=1819577

        if self.m_elicitation_method == "replacements":
            self.last_deletes_dict = dict() #key: statement_uid, value: previous_reference
            self.last_delete_timestamp_dict = dict() #key: statement_uid, value: timestamp of last delete
        if self.m_elicitation_method == "session":
            #sessions are per statements. Use statement_id as key
            self.session_last_edit_timestamp = dict() #key: statement_uid, value: timestamp of last edit (create or delete)
            self.session_current_references = defaultdict(set) #key: statement_uid, value: all current reference domains
            self.session_all_references = defaultdict(set) #key: statement_uid, value: set of all reference domains used in session

    #some of these helper methods for transitioning from networkx to graph-tool are written with co-pilot.
    def _get_or_add_vertex(self, domain: str):
        vertex = self.domain_to_vertex.get(domain)
        if vertex is None:
            # A previously contracted domain will reappear here as a fresh vertex.
            vertex = self.reference_graph.add_vertex()
            self.domain_to_vertex[domain] = vertex
            self.vertex_domain[vertex] = domain
        return vertex

    def _get_domain_for_vertex(self, vertex) -> str:
        return self.vertex_domain[vertex]

    def _add_or_increment_edge(self, source_vertex, target_vertex, delta: int):
        if delta <= 0:
            return
        edge = self.reference_graph.edge(source_vertex, target_vertex)
        if edge is None:
            edge = self.reference_graph.add_edge(source_vertex, target_vertex)
            self.edge_weight[edge] = delta
            self.edge_accepted[edge] = False
        else:
            self.edge_weight[edge] += delta

    def _rebuild_domain_to_vertex_index(self):
        rebuilt_index: dict[str, object] = {}
        for vertex in self.reference_graph.vertices():
            domain = self.vertex_domain[vertex]
            if domain in rebuilt_index and int(rebuilt_index[domain]) != int(vertex):
                raise RuntimeError(f"Duplicate domain vertex detected for domain '{domain}'.")
            rebuilt_index[domain] = vertex
        self.domain_to_vertex = rebuilt_index

    def _contract_low_mass_domains(self, mass_per_domain_dict: dict[str, int]):
        self.contracted_mass_last_step = 0.0
        if self.low_mass_contraction_threshold is None or self.low_mass_contraction_threshold <= 0:
            return

        if self.min_graph_size_for_contraction is not None and self.min_graph_size_for_contraction > 0:
            current_domain_vertex_count = self.reference_graph.num_vertices()
            if self.contracted_domain_name in self.domain_to_vertex:
                current_domain_vertex_count -= 1
            if current_domain_vertex_count < self.min_graph_size_for_contraction:
                return

        aggregate_vertex = self._get_or_add_vertex(self.contracted_domain_name)
        aggregate_index = int(aggregate_vertex)

        vertices_to_contract = []
        for vertex in self.reference_graph.vertices():
            vertex_index = int(vertex)
            if vertex_index == aggregate_index:
                continue
            domain = self.vertex_domain[vertex]
            if mass_per_domain_dict.get(domain, 0) < self.low_mass_contraction_threshold:
                vertices_to_contract.append(vertex_index)

        if not vertices_to_contract:
            return

        contracted_mass_this_step = 0.0
        for vertex_index in vertices_to_contract:
            vertex = self.reference_graph.vertex(vertex_index)
            domain = self.vertex_domain[vertex]
            contracted_mass_this_step += float(mass_per_domain_dict.get(domain, 0))
        self.contracted_mass_last_step = contracted_mass_this_step
        self.contracted_mass_cumulative += contracted_mass_this_step

        vertices_to_contract_set = set(vertices_to_contract)

        # Re-route all affected edges once to avoid double counting.
        rerouted_edge_deltas = defaultdict(int)
        for edge in self.reference_graph.edges():
            source_index = int(edge.source())
            target_index = int(edge.target())

            mapped_source_index = aggregate_index if source_index in vertices_to_contract_set else source_index
            mapped_target_index = aggregate_index if target_index in vertices_to_contract_set else target_index

            if mapped_source_index == source_index and mapped_target_index == target_index:
                continue

            rerouted_edge_deltas[(mapped_source_index, mapped_target_index)] += int(self.edge_weight[edge])

        for (mapped_source_index, mapped_target_index), delta in rerouted_edge_deltas.items():
            self._add_or_increment_edge(
                self.reference_graph.vertex(mapped_source_index),
                self.reference_graph.vertex(mapped_target_index),
                delta,
            )

        vertices_to_contract_desc = sorted(vertices_to_contract, reverse=True)
        self.reference_graph.remove_vertex(vertices_to_contract_desc, fast=False)

        self._rebuild_domain_to_vertex_index()
        print(
            f"Contracted {len(vertices_to_contract)} low-mass domains (< {self.low_mass_contraction_threshold}) into {self.contracted_domain_name}."
        )

    def _select_kth_smallest_linear(self, values: list[int], k: int) -> int:
        """Select k-th smallest element in-place using median-of-medians."""
        if not values:
            raise ValueError("values must not be empty")
        if k < 0 or k >= len(values):
            raise IndexError("k is out of range")

        def _sort_small(left: int, right: int) -> None:
            for i in range(left + 1, right + 1):
                cur = values[i]
                j = i - 1
                while j >= left and values[j] > cur:
                    values[j + 1] = values[j]
                    j -= 1
                values[j + 1] = cur

        def _partition_three_way(left: int, right: int, pivot_value: int) -> tuple[int, int]:
            lt = left
            i = left
            gt = right
            while i <= gt:
                if values[i] < pivot_value:
                    values[lt], values[i] = values[i], values[lt]
                    lt += 1
                    i += 1
                elif values[i] > pivot_value:
                    values[gt], values[i] = values[i], values[gt]
                    gt -= 1
                else:
                    i += 1
            return lt, gt

        def _choose_pivot_value(left: int, right: int) -> int:
            while True:
                size = right - left + 1
                if size <= 5:
                    _sort_small(left, right)
                    return values[left + size // 2]

                # Move each 5-group median to the front of the current range.
                write = left
                start = left
                while start <= right:
                    group_right = min(start + 4, right)
                    _sort_small(start, group_right)
                    median_index = start + (group_right - start) // 2
                    values[write], values[median_index] = values[median_index], values[write]
                    write += 1
                    start += 5

                right = write - 1

        left = 0
        right = len(values) - 1
        while True:
            if left == right:
                return values[left]

            pivot_value = _choose_pivot_value(left, right)
            low_end, high_start = _partition_three_way(left, right, pivot_value)

            if k < low_end:
                right = low_end - 1
            elif k > high_start:
                left = high_start + 1
            else:
                return pivot_value

    def _compute_bottom_edge_weight_cutoff(self) -> int | None:
        """Find the x%-quantile edge weight cutoff without sorting all edges."""
        fraction = self.heuristic_discard_bottom_edge_fraction
        if fraction is None or fraction <= 0.0:
            return None

        edge_count = self.reference_graph.num_edges()
        if edge_count == 0:
            return None

        weights = [int(self.edge_weight[edge]) for edge in self.reference_graph.edges()]
        if not weights:
            return None

        cutoff_index = int(fraction * len(weights))
        cutoff_index = min(cutoff_index, len(weights) - 1)
        return self._select_kth_smallest_linear(weights, cutoff_index)

    def _has_path_accepted(self, pref_graph: GraphView, source_index: int, target_index: int, max_depth=5):
        """Check if source can reach target on accepted-edge view within max_depth."""
        if not isinstance(pref_graph, GraphView):
            raise TypeError("_has_path_accepted expects a GraphView filtered to accepted edges.")

        if source_index == target_index:
            return True

        # Explicitly disable edge weights to keep this a hop-count search.
        distance = shortest_distance(
            pref_graph,
            source=pref_graph.vertex(source_index),
            target=pref_graph.vertex(target_index),
            weights=None,
            max_dist=max_depth,
        )
        return distance <= max_depth


    def process_dead_session(self, uid):
        bad_domains = self.session_all_references[uid] - self.session_current_references[uid]
        #add preferences
        #O(n2) but ok
        preferences = [(bad_domain, good_domain) for bad_domain in bad_domains for good_domain in self.session_current_references[uid]]

        del self.session_last_edit_timestamp[uid]
        del self.session_current_references[uid]
        del self.session_all_references[uid]

        return preferences

    def calculate_diff(self):
        edits = self.datahandler.query_duckdb_df(all_edits_query) # edits with ascending timestamp

        required_columns = {"timestamp", "action", "reference", "entity_id", "property_id", "value_id"}
        if edits is None or edits.empty or not required_columns.issubset(set(edits.columns)):
            # No usable reference edits found in this slice.
            print("Found 0 usable reference edits in this timestep.")
            return []

        allowed_prefixes = ["Q", "http", "www", "ISBN", "PMID", "DOI"] #TODO: check if these are good enough to capture refs.
        edits["reference"] = edits["reference"].fillna("").astype(str).str.strip('"')
        edits["reference"] = edits["reference"].str.replace(" ", "%20")
        edits = edits[edits["reference"].str.startswith(tuple(allowed_prefixes))]

        if edits.empty:
            print("Found 0 usable reference edits in this timestep.")
            return []
        
        #create statement_uid
        #The statement has multiple ref_property_ids (data, content, subsite etc)
        # -> disregard the other changes for now (i.e. remove dublicate adds) //no group by because we want sequence of adds / deletes
        #NOTE: there may be different ref_property_id s. These contain dates etc
        edits = edits.assign(
            statement_uid=(
                edits["entity_id"].astype(str)
                + "_"
                + edits["property_id"].astype(str)
                + "_"
                + edits["value_id"].astype(str)
            )
        )

        if self.m_elicitation_method == "session":
            preferences = list()
            for row in edits.itertuples(index=False):
                uid = row.statement_uid
                domain = domain_from_reference(row.reference)
                time = row.timestamp
                #check for unhandeld dead session
                if uid in self.session_last_edit_timestamp and self.session_last_edit_timestamp[uid] + self.m_session_death_time < time:
                    preferences.extend(self.process_dead_session(uid))
                #create / update session
                self.session_last_edit_timestamp[uid] = time #TODO: make sure these are real timesteps

                #TODO: Use ReferenceExistencePerStatementCountDict.cache_dict[value_id] for a count in the calculate_diff method and add NONE whenever it is 0

                if row.action == "CREATE": 
                    self.session_current_references[uid].add(domain)
                elif row.action == "DELETE":
                    # NOTE: test this: Keep current references in sync with edit stream.
                    self.session_current_references[uid].discard(domain) #doing this means that we also get to know about the initial reference if it was removed.
                self.session_all_references[uid].add(domain)

            #deal with dead session after this timestep
            for uid in self.session_last_edit_timestamp.copy():
                if self.session_last_edit_timestamp[uid] + self.m_session_death_time < self.datahandler.get_current_end_timestamp():
                    preferences.extend(self.process_dead_session(uid))

            print(f"Found {len(preferences)} prefs in this timestep.")
            return preferences

        if self.m_elicitation_method == "replacements":
            replacements : list[tuple] = list()

            #assumes the rows are ordered by timestamp asc.
            for row in edits.itertuples(index=False):
                uid = row.statement_uid
                domain = domain_from_reference(row.reference)
                if row.action == "CREATE":
                    if uid in self.last_deletes_dict:
                        if self.last_deletes_dict[uid] == domain:
                            #delete followed by create with same reference. Ignore and remove from deletes dict.
                            del self.last_deletes_dict[uid]
                            del self.last_delete_timestamp_dict[uid]
                        else:
                            if self.last_delete_timestamp_dict[uid] + self.time_delta_for_expressing_preference >= row.timestamp:
                                replacements.append((self.last_deletes_dict[uid], domain))
                            del self.last_deletes_dict[uid]
                            del self.last_delete_timestamp_dict[uid]
                    else:
                        self.last_creates_dict[uid] = domain
                        self.last_create_timestamp_dict[uid] = row.timestamp
                else:# row.action == "DELETE":
                    #TODO: use last creates dict? to match CRE DELs? -> EXAMINE
                    if uid in self.last_creates_dict:
                        if self.last_creates_dict[uid] == domain:
                            #create followed by delete with same reference. Discard create but memorize delete.
                            del self.last_creates_dict[uid]
                            del self.last_create_timestamp_dict[uid]
                        else:
                            if self.last_create_timestamp_dict[uid] + self.time_delta_for_expressing_preference >= row.timestamp:
                                replacements.append((self.last_creates_dict[uid], domain))
                            del self.last_creates_dict[uid]
                            del self.last_create_timestamp_dict[uid]
                    else:
                        self.last_deletes_dict[uid] = domain
                        self.last_delete_timestamp_dict[uid] = row.timestamp
            
            print(f"Found {len(replacements)} replacements in this timestep.")

            #clean up dicts based on timedelta
            for uid in self.last_delete_timestamp_dict.copy():
                if self.last_delete_timestamp_dict[uid] + self.time_delta_for_expressing_preference < self.datahandler.get_current_end_timestamp():
                    del self.last_deletes_dict[uid]
                    del self.last_delete_timestamp_dict[uid]
            for uid in self.last_create_timestamp_dict.copy():
                if self.last_create_timestamp_dict[uid] + self.time_delta_for_expressing_preference < self.datahandler.get_current_end_timestamp():
                    del self.last_creates_dict[uid]
                    del self.last_create_timestamp_dict[uid]

            return replacements

    def calculate(self):
        replacements = self.calculate_diff()

        #get results of counts per domain
        ref_per_domain = self.count_of_references_per_domain.get_last_value()
        if (
            ref_per_domain is None
            or ref_per_domain.empty
            or "domain" not in ref_per_domain.columns
            or "count" not in ref_per_domain.columns
        ):
            print("No reference counts per domain available in this timestep.")
            self.dominated_fraction = 0.0
            self.dominating_fraction = 0.0
            self.contested_fraction = 0.0
            empty_ref_per_domain = pd.DataFrame(columns=["domain", "count"])
            self.write_result(set(), set(), set(), empty_ref_per_domain)
            return

        # if self.heuristic_fractional_cutoff is not None or self.m_classification_method == "weight_flow":
        #we now always use this for cutting of domains with too little mass
        mass_per_domain_dict = dict(zip(ref_per_domain["domain"], ref_per_domain["count"]))

        total_refs = ref_per_domain["count"].sum()

        #1. MARGIN GRAPH
        #Batch updates to reduce Python <-> C++ boundary calls.
        replacement_edge_deltas = defaultdict(int)
        for old_domain, new_domain in replacements:
            if old_domain != new_domain:
                replacement_edge_deltas[(old_domain, new_domain)] += 1

        for (old_domain, new_domain), delta in replacement_edge_deltas.items():
            old_vertex = self._get_or_add_vertex(old_domain)
            new_vertex = self._get_or_add_vertex(new_domain)
            self._add_or_increment_edge(old_vertex, new_vertex, delta)

        # Contract small domains after updating edge weights for this timestep.
        self._contract_low_mass_domains(mass_per_domain_dict)
            
        #2. PREFERENCE GRAPH - mark edges as "accepted" instead of copying graph
        #Initialize all edges as not accepted
        self.edge_accepted.a = False
        
        if self.m_pref_graph_method == "discard_edge_heuristic":
            bottom_weight_cutoff = None
            if self.heuristic_discard_bottom_edge_fraction is not None:
                bottom_weight_cutoff = self._compute_bottom_edge_weight_cutoff()
            #Mark edges that pass the heuristic as "accepted"
            for edge in self.reference_graph.edges():
                source = edge.source()
                target = edge.target()
                source_domain = self._get_domain_for_vertex(source)
                weight = self.edge_weight[edge]
                if bottom_weight_cutoff is not None and weight < bottom_weight_cutoff:
                    continue
                if self.heuristic_absolute_cuttoff is not None and weight < self.heuristic_absolute_cuttoff:
                    continue
                if self.heuristic_fractional_cutoff is not None:
                    if weight < self.heuristic_fractional_cutoff * mass_per_domain_dict.get(source_domain, 0):
                        continue
                
                reverse_edge = self.reference_graph.edge(target, source)
                reverse_weight = self.edge_weight[reverse_edge] if reverse_edge is not None else 0

                if weight > reverse_weight * self.heuristic_fraction_of_reverse_to_add:
                    #Mark this edge as accepted to indicate domination
                    self.edge_accepted[edge] = True
        elif self.m_pref_graph_method == "keep_all_edges": 
            #Mark all edges as accepted
            self.edge_accepted.a = True

        pref_graph = GraphView(self.reference_graph, efilt=self.edge_accepted)


        dominated_domains = set()
        dominating_domains = set()
        contested_domains = set()
        if self.m_classification_method == "weight_flow":
            temp_skip_num = 0
            #2. CLASSIFICATION BASED ON WEIGHT FLOW
            #calculate net outgoing weight for each node
            out_weight_map = self.reference_graph.degree_property_map("out", weight=self.edge_weight)
            in_weight_map = self.reference_graph.degree_property_map("in", weight=self.edge_weight)
            for node in self.reference_graph.iter_vertices():
                out_weight = out_weight_map[node]
                in_weight = in_weight_map[node]
                node_domain = self.vertex_domain[node]

                if out_weight + in_weight < self.m_weigth_flow_skip_if_too_few_changes_wrt_total * mass_per_domain_dict.get(node_domain, 0):
                    temp_skip_num += 1
                    continue

                out_frac = out_weight / (out_weight + in_weight) if (out_weight + in_weight) > 0 else 0.0
                in_frac = in_weight / (out_weight + in_weight) if (out_weight + in_weight) > 0 else 0.0

                if out_frac > self.m_weight_flow_threshold:
                    dominated_domains.add(node_domain)
                elif in_frac > self.m_weight_flow_threshold:
                    dominating_domains.add(node_domain)
                else:
                    contested_domains.add(node_domain)
            # print(f"Skipped {temp_skip_num} domains with too few changes compared to total count.")
        
        if self.m_classification_method == "simple_degree":
            #   - CLEARLY DOMINATING: out_degree = 0 and in_degree > 0
            #   - CLEARLY DOMINATED:  in_degree = 0 and out_degree > 0
            #   - CONTESTED:          in_degree > 0 and out_degree > 0
            # Nodes not with accepted edges are UNCLASSIFIED:
            # they lack sufficient replacement data to make any claim.
            #
            # This is the simplest classifier consistent with pairwise
            # dominance and is equivalent to identifying extremes of the
            # Copeland score in social choice theory (nodes with only wins
            # or only losses).
            out_degree_map = pref_graph.degree_property_map("out")
            in_degree_map = pref_graph.degree_property_map("in")
            for node in pref_graph.iter_vertices():
                out_degree = out_degree_map[node]
                in_degree = in_degree_map[node]
                if out_degree == 0 and in_degree == 0:
                    continue
                node_domain = self.vertex_domain[node]
                if out_degree == 0 and in_degree > 0:
                    dominating_domains.add(node_domain)
                elif in_degree == 0 and out_degree > 0:
                    dominated_domains.add(node_domain)
                elif in_degree > 0 and out_degree > 0:
                    contested_domains.add(node_domain)

        if self.m_classification_method == "simple_with_path":
            #   - CLEARLY DOMINATING: out_degree = 0 and in_degree > 0
            #   - CLEARLY DOMINATED:  in_degree = 0 and out_degree > 0
            #   - CONTESTED:          in_degree > 0 and out_degree > 0 and beating all incoming edges with rebutting paths
            out_degree_map = pref_graph.degree_property_map("out")
            in_degree_map = pref_graph.degree_property_map("in")
            for node in pref_graph.iter_vertices():
                out_degree = out_degree_map[node]
                in_degree = in_degree_map[node]
                if out_degree == 0 and in_degree == 0:
                    continue
                node_domain = self.vertex_domain[node]
                if out_degree == 0 and in_degree > 0:
                    dominating_domains.add(node_domain)
                elif in_degree == 0 and out_degree > 0:
                    dominated_domains.add(node_domain)
                elif in_degree > 0 and out_degree > 0:
                    #refined contested condition: check if node can beat all incoming edges with paths
                    if all(self._has_path_accepted(pref_graph, source_index=dominator, target_index=node) for dominator in pref_graph.iter_out_neighbors(node)):
                        contested_domains.add(node_domain)
                    else:
                        dominated_domains.add(node_domain)

        print(f"Found {len(dominated_domains)} clearly dominated domains in this timestep.")
        print(f"Found {len(dominating_domains)} clearly dominating domains in this timestep.")
        print(f"Found {len(contested_domains)} contested domains in this timestep.")
        self.dominated_fraction = ref_per_domain[ref_per_domain['domain'].isin(dominated_domains)]['count'].sum() / total_refs if total_refs > 0 else 0.0
        self.dominating_fraction = ref_per_domain[ref_per_domain['domain'].isin(dominating_domains)]['count'].sum() / total_refs if total_refs > 0 else 0.0
        self.contested_fraction = ref_per_domain[ref_per_domain['domain'].isin(contested_domains)]['count'].sum() / total_refs if total_refs > 0 else 0.0
        print(f"Fraction of references that are clearly dominated: {self.dominated_fraction:.2%}")
        print(f"Fraction of references that are clearly dominating: {self.dominating_fraction:.2%}")
        print(f"Fraction of references that are contested: {self.contested_fraction:.2%}")

        self.write_result(dominated_domains, dominating_domains, contested_domains, ref_per_domain)

        # CSV logging: largest k contested, clearly dominating and clearly dominated domains for later analysis.
        ## For QID domains, add a human-readable Wikidata label in the CSV export.
        # k = 30
        # snapshot = f"{self.datahandler.get_current_end_timestamp().year}_{self.datahandler.get_current_end_timestamp().month}"
        # dominated_df = ref_per_domain[ref_per_domain["domain"].isin(dominated_domains)].sort_values("count", ascending=False).head(k).copy()
        # dominating_df = ref_per_domain[ref_per_domain["domain"].isin(dominating_domains)].sort_values("count", ascending=False).head(k).copy()
        # contested_df = ref_per_domain[ref_per_domain["domain"].isin(contested_domains)].sort_values("count", ascending=False).head(k).copy()
        # all_export_qids = {
        #     domain
        #     for domain in pd.concat([dominated_df["domain"], dominating_df["domain"], contested_df["domain"]], ignore_index=True).dropna().unique()
        #     if isinstance(domain, str) and domain.startswith("Q")
        # }
        # qid_to_label = get_wikidata_titles(sorted(all_export_qids))
        # def add_export_columns(df: pd.DataFrame) -> pd.DataFrame:
        #     enriched = df.copy()
        #     enriched.insert(0, "snapshot", snapshot)
        #     enriched["domain_label"] = enriched["domain"].map(lambda d: qid_to_label.get(d) if isinstance(d, str) and d.startswith("Q") else "")
        #     return enriched[["snapshot", "domain", "domain_label", "count"]]
        # dominated_out = add_export_columns(dominated_df)
        # dominating_out = add_export_columns(dominating_df)
        # contested_out = add_export_columns(contested_df)
        # dominated_path = f"reference_ranking_misc/dominated_largest_{self.m_classification_method}_{self.m_pref_graph_method}.csv"
        # dominating_path = f"reference_ranking_misc/dominating_largest_{self.m_classification_method}_{self.m_pref_graph_method}.csv"
        # contested_path = f"reference_ranking_misc/contested_largest_{self.m_classification_method}_{self.m_pref_graph_method}.csv"
        # dominated_out.to_csv(dominated_path, mode="a", index=False, header=not os.path.exists(dominated_path))
        # dominating_out.to_csv(dominating_path, mode="a", index=False, header=not os.path.exists(dominating_path))
        # contested_out.to_csv(contested_path, mode="a", index=False, header=not os.path.exists(contested_path))
       

    def write_result(self, dominated, dominating, contested, ref_per_domain: pd.DataFrame):
        settings_string = f"{self.m_classification_method}_{self.m_pref_graph_method}"
        if self.heuristic_discard_bottom_edge_fraction is not None:
            bottom_pct = int(self.heuristic_discard_bottom_edge_fraction * 100)
            settings_string += f"_dis_{bottom_pct}pct"
        self.datahandler.write_global_metric_value(self.metric_name + f"_{settings_string}" + f"_dominated", float(self.dominated_fraction))
        self.datahandler.write_global_metric_value(self.metric_name + f"_{settings_string}" + f"_dominating", float(self.dominating_fraction))
        self.datahandler.write_global_metric_value(self.metric_name + f"_{settings_string}" + f"_contested", float(self.contested_fraction))
        self.datahandler.write_global_metric_value(
            self.metric_name + f"_{settings_string}" + "_contracted_mass_total",
            float(self.contracted_mass_cumulative),
        )
        self.datahandler.write_global_metric_value(
            self.metric_name + f"_{settings_string}" + "_contracted_mass_step",
            float(self.contracted_mass_last_step),
        )
        self.write_domain_classification_results(settings_string, dominated, dominating, contested, ref_per_domain)
        # Save graph data for later analysis (graph-tool)
        # self.reference_graph.save(f"reference_ranking_misc/reference_replacement_graph{self.datahandler.get_current_end_timestamp().year}_{self.datahandler.get_current_end_timestamp().month}_{self.datahandler.get_current_end_timestamp().day}.gt")

    def write_domain_classification_results(
        self,
        settings_string: str,
        dominated: set[str],
        dominating: set[str],
        contested: set[str],
        ref_per_domain: pd.DataFrame,
    ):
        if self.largest_domains_to_write_metric <= 0:
            return

        if len(dominated) == 0 and len(dominating) == 0 and len(contested) == 0:
            return

        # Build a domain->class map with explicit precedence (dominating > dominated > contested).
        class_map = {domain: 0.0 for domain in contested}
        class_map.update({domain: -1.0 for domain in dominated})
        class_map.update({domain: 1.0 for domain in dominating})

        top_domains = (
            ref_per_domain[["domain", "count"]]
            .dropna(subset=["domain"])
            .sort_values("count", ascending=False)
            .head(self.largest_domains_to_write_metric)
            .copy()
        )

        if top_domains.empty:
            return

        top_domains["classification_value"] = top_domains["domain"].map(class_map)
        top_domains = top_domains.dropna(subset=["classification_value"])

        if top_domains.empty:
            return

        metric_name = self.metric_name + f"_{settings_string}_domain_classification"
        self.datahandler.write_metric_on_string(metric_name, top_domains, "domain", "classification_value")

    def log_memory(self):
        tracked_attrs = {
            "reference_graph": self.reference_graph,
        }

        if self.m_elicitation_method == "replacements":
            tracked_attrs["last_deletes_dict_size"] = self.last_deletes_dict
            tracked_attrs["last_delete_timestamp_dict_size"] = self.last_delete_timestamp_dict
        if self.m_elicitation_method == "session":
            tracked_attrs["session_last_edit_timestamp_size"] = self.session_last_edit_timestamp
            tracked_attrs["session_current_references_size"] = self.session_current_references
            tracked_attrs["session_all_references_size"] = self.session_all_references

        log_memory_snapshot(self.metric_name, tracked_attrs)

    def save_cache(self):
        print("Warning: (ReferenceReplacement) save_cache is not implemented. This metric currently only supports in-memory state.")
