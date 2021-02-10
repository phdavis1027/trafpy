from trafpy.generator.src import tools
from trafpy.generator.src.dists import val_dists, node_dists

import numpy as np
import time
from collections import defaultdict # use for initialising arbitrary length nested dict
import json
import copy
import random
from progress.bar import ShadyBar
# from progress.spinner import Spinner







class FlowGenerator:
    def __init__(self,
                 eps,
                 node_dist,
                 flow_size_dist,
                 interarrival_time_dist,
                 num_demands_factor=500,
                 network_load_config=None,
                 min_last_demand_arrival_time=None,
                 auto_node_dist_correction=False,
                 print_data=False):
        '''
        Args:
            network_load_config (dict): Dict of form {'network_rate_capacity': <int/float>, 'target_load_fraction': <float>, 'disable_timeouts': <bool>, 'return_new_interarrival_time_dist': <bool>},
                where network_rate_capacity is the maximum rate (in e.g. Gbps) at which
                information can be reliably transmitted over the communication network
                which the demand data will be inserted into, and where target_load_fraction
                is the fraction of the network rate capacity being requested by the demands
                (e.g. target_load_fraction=0.75 would generate demands which request
                a load that is 75% of the network rate capacity from the first to 
                the last demand arriving). disable_timeouts defines whether or not 
                to stop looping when trying to meet specified network load. return_new_interarrival_time_dist
                defines whether or not to return the new interarrival time dist which
                was adjusted to meet the network node requested. If None, won't adjust
                inter arrival time dist at all to meet network load.
            auto_node_dist_correction (bool): Set to True if you want TrafPy to
                automatically make invalid node distributions valid. If True, invalid
                node distributions where more load is being assigned to a end point
                link than the end point link has bandwidth will be changed by 
                removing the invalid end point link load to its maximum 1.0 load
                and distributing the removed load across all other valid links
                uniformly.

        '''
        self.started = time.time()

        self.eps = eps
        self.node_dist = node_dist
        self.flow_size_dist = flow_size_dist
        self.interarrival_time_dist = interarrival_time_dist
        self.num_demands_factor = num_demands_factor
        self.network_load_config = network_load_config
        self.min_last_demand_arrival_time = min_last_demand_arrival_time
        self.auto_node_dist_correction = auto_node_dist_correction
        self.print_data = print_data

        self.num_nodes, self.num_pairs, self.node_to_index, self.index_to_node = tools.get_network_params(self.eps)
        self.num_demands = int(self.num_pairs * num_demands_factor)

        if self.network_load_config is not None:
            if self.network_load_config['target_load_fraction'] > 0.95:
                raise Exception('Target load fraction {} is invalid. Must be <= 0.95.'.format(self.network_load_config['target_load_fraction']))

    def create_flow_centric_demand_data(self):
        # flow ids
        flow_ids = ['flow_'+str(i) for i in range(self.num_demands)]
        establish = np.concatenate((np.ones((int(len(flow_ids)))), 
                                    np.zeros((int(len(flow_ids)))))).astype(int)

        # flow sizes
        flow_sizes = val_dists.gen_rand_vars_from_discretised_dist(unique_vars=list(self.flow_size_dist.keys()),
                                                                   probabilities=list(self.flow_size_dist.values()),
                                                                   num_demands=self.num_demands)

        # flow interarrival times
        interarrival_times = val_dists.gen_rand_vars_from_discretised_dist(unique_vars=list(self.interarrival_time_dist.keys()),
                                                                           probabilities=list(self.interarrival_time_dist.values()),
                                                                           num_demands=self.num_demands)
        if self.network_load_config is not None:
            # adjust overall interarrival time dist until overall load <= user-specified load
            interarrival_times = self._adjust_demand_load(flow_sizes,
                                                          interarrival_times)

        # corresponding flow event (arrival) times
        event_times = tools.gen_event_times(interarrival_times)
        index, event_times_sorted = np.argsort(event_times), np.sort(event_times)

        # pack the flows into src-dst pairs to meet src-dst pair load config requirements of node_dist
        packer = FlowPacker(self,
                            self.eps,
                            self.node_dist,
                            flow_ids,
                            flow_sizes,
                            interarrival_times,
                            network_load_config=self.network_load_config,
                            auto_node_dist_correction=self.auto_node_dist_correction)
        packed_flows = packer.pack_the_flows()

        # compile packed flows into demand_data dict ordered in terms of arrival time
        demand_data = {'flow_id': [],
                        'sn': [],
                        'dn': [],
                        'flow_size': [],
                        'event_time': event_times_sorted,
                        'establish': establish,
                        'index': index}
        for flow in packed_flows.keys():
            demand_data['flow_id'].append(flow)
            demand_data['sn'].append(packed_flows[flow]['src'])
            demand_data['dn'].append(packed_flows[flow]['dst'])
            demand_data['flow_size'].append(packed_flows[flow]['size'])

        if self.min_last_demand_arrival_time is not None:
            # duplicate flows until get duration >= user-specified duration
            # duplication_spinner = Spinner('Duplicating flows ')
            while max(demand_data['event_time']) < self.min_last_demand_arrival_time:
                demand_data = duplicate_demands_in_demand_data_dict(demand_data, method='all_eps', eps=self.eps)
                # duplication_spinner.next()

        return demand_data


    def _calc_overall_load_rate(self, flow_sizes, interarrival_times, bidirectional_links=True):
        '''
        If flow connections are bidirectional_links, 1 flow takes up 2 endpoint links (the
        source link and the destination link), therefore effecitvely takes up load rate
        2*flow_size*duration bandwidth. If not bidriectional, only takes up
        1*flow_size*duration since only occupies bandwidth for 1 of these links.

        Returns load rate (info units per unit time).
        '''
        info_arrived = self._calc_total_info_arrived(flow_sizes)
        first_flow_arrival_time, last_flow_arrival_time = self._get_first_last_flow_arrival_times(interarrival_times)
        duration = last_flow_arrival_time - first_flow_arrival_time
        if bidirectional_links:
            # 1 flow occupies 2 endpoint links therefore has 2*flow_size load
            return 2*info_arrived/duration
        else:
            return info_arrived/duration

    def _calc_total_info_arrived(self, flow_sizes):
        return np.sum(flow_sizes)

    def _get_first_last_flow_arrival_times(self, interarrival_times):
        event_times = tools.gen_event_times(interarrival_times)
        return min(event_times), max(event_times)

    def _change_interarrival_times_by_factor(self, interarrival_times, factor):
        '''Updates self.interarrival_time_dist by a specified factor and returns new interarrival times.'''
        new_interarrival_time_dist = {}
        for rand_var, prob in self.interarrival_time_dist.items():
            new_rand_var = rand_var * factor
            new_interarrival_time_dist[new_rand_var] = prob

        # update interarrival time dist
        self.interarrival_time_dist = new_interarrival_time_dist

        # gen new interarrival times
        interarrival_times = val_dists.gen_rand_vars_from_discretised_dist(unique_vars=list(self.interarrival_time_dist.keys()),
                                                                           probabilities=list(self.interarrival_time_dist.values()),
                                                                           num_demands=self.num_demands)

        return interarrival_times

    def _increase_demand_load_to_target(self, flow_sizes, interarrival_times, increment_factor=0.5):
        '''
        Updates self.interarrival_time_dist and returns new interarrival times
        such that overall load > target load.
        '''
        load_rate = self._calc_overall_load_rate(flow_sizes, interarrival_times)
        load_fraction = load_rate / self.network_load_config['network_rate_capacity']

        while load_fraction < self.network_load_config['target_load_fraction']:
            # decrease interarrival times to increase load
            interarrival_times = self._change_interarrival_times_by_factor(interarrival_times, increment_factor)

            # calc new load rate
            load_rate = self._calc_overall_load_rate(flow_sizes, interarrival_times)
            load_fraction = load_rate / self.network_load_config['network_rate_capacity']

        return interarrival_times

    def _decrease_demand_load_to_target(self, flow_sizes, interarrival_times, increment_factor=1.001):
        '''
        Updates self.interarrival_time_dist and returns new interarrival times
        such that overall load <= target load.
        '''
        load_rate = self._calc_overall_load_rate(flow_sizes, interarrival_times)
        load_fraction = load_rate / self.network_load_config['network_rate_capacity']

        while load_fraction > self.network_load_config['target_load_fraction']:
            # increase interarrival times to decrease load
            interarrival_times = self._change_interarrival_times_by_factor(interarrival_times, increment_factor)

            # calc new load rate
            load_rate = self._calc_overall_load_rate(flow_sizes, interarrival_times)
            load_fraction = load_rate / self.network_load_config['network_rate_capacity']

        return interarrival_times


    def _adjust_demand_load(self,
                            flow_sizes,
                            interarrival_times):
        # adjust to get load fraction > target load fraction (using big factor to decrease search time)
        interarrival_times = self._increase_demand_load_to_target(flow_sizes, interarrival_times)

        # adjust back down to get load fraction <= target load fraction (using small factor to get high search resolution)
        interarrival_times = self._decrease_demand_load_to_target(flow_sizes, interarrival_times)

        return interarrival_times






class FlowPacker:
    def __init__(self,
                 generator,
                 eps,
                 node_dist,
                 flow_ids,
                 flow_sizes,
                 flow_interarrival_times,
                 network_load_config=None,
                 auto_node_dist_correction=False,
                 print_data=False):
        self.generator = generator
        self.eps = eps
        self.node_dist = copy.deepcopy(node_dist)
        self.flow_ids = flow_ids
        self.flow_sizes = flow_sizes
        self.flow_interarrival_times = flow_interarrival_times
        self.network_load_config = network_load_config
        self.auto_node_dist_correction = auto_node_dist_correction
        self.print_data = print_data

        self.reset()
        self._check_node_dist_valid_for_this_load()

    def reset(self):
        # init dict in which flows will be packed into src-dst pairs
        nested_dict = lambda: defaultdict(nested_dict)
        self.packed_flows = nested_dict()

        # want to pack largest flows first -> re-organise flows into descending order (will shuffle later so maintain random flow sizes of arrivals)
        self.flow_sizes[::-1].sort()
        for idx in range(len(self.flow_ids)):
            self.packed_flows[self.flow_ids[idx]] = {'size': self.flow_sizes[idx],
                                                     'src': None,
                                                     'dst': None}

        # calc overall load rate
        self.load_rate = self.generator._calc_overall_load_rate(self.flow_sizes, self.flow_interarrival_times)

        # calc target load rate of each src-dst pair
        self.num_nodes, self.num_pairs, self.node_to_index, self.index_to_node = tools.get_network_params(self.eps)
        self.pair_prob_dict = node_dists.get_pair_prob_dict_of_node_dist_matrix(self.node_dist, self.eps)
        self.pair_target_load_rate_dict = {pair: frac*self.load_rate for pair, frac in self.pair_prob_dict.items()}

        # calc target total info to pack into each src-dst pair
        flow_event_times = tools.gen_event_times(self.flow_interarrival_times)
        self.duration = max(flow_event_times) - min(flow_event_times)
        self.pair_target_total_info_dict = {pair: load_rate*self.duration for pair, load_rate in self.pair_target_load_rate_dict.items()}

        # init current total info packed into each src-dst pair and current distance from target info
        self.pair_current_total_info_dict = {pair: 0 for pair in self.pair_prob_dict.keys()}
        self.pair_current_distance_from_target_info_dict = {pair: self.pair_target_total_info_dict[pair]-self.pair_current_total_info_dict[pair] for pair in self.pair_prob_dict.keys()}

        # calc max total info during simulation per end point and initialise end point total info tracker
        self.max_total_ep_info = self.network_load_config['ep_link_capacity'] * self.duration
        self.ep_total_infos = {ep: 0 for ep in self.eps}


    def pack_the_flows(self):
        packer_bar = ShadyBar('Packing flows ', max=len(self.packed_flows.keys()))
        for flow in self.packed_flows.keys():
            chosen_pair = None

            # first 
            # try to allocate flow to pair which is currently furthest away from its target load
            sorted_pairs = sorted(self.pair_current_distance_from_target_info_dict.items(), key = lambda x: x[1], reverse=True) # sorts into descending order
            for p in sorted_pairs:
                pair = p[0]
                if self.pair_current_distance_from_target_info_dict[pair] - self.packed_flows[flow]['size'] < 0:
                    # would exceed pair's target load by allocating to this pair, try next pair
                    continue
                else:
                    chosen_pair = pair
                    break
            # second
            if chosen_pair is None:
                # cannot allocate flow to any pair without exceeding its target load. Try to allocate to most 'hot' pairs without exceedeing maximum end point bandwidths
                sorted_pairs = sorted(self.pair_target_total_info_dict.items(), key = lambda x: x[1], reverse=True) # sorts into descending order
                for p in sorted_pairs:
                    pair = p[0]
                    ep1, ep2 = json.loads(pair)[0], json.loads(pair)[1]
                    if self.ep_total_infos[ep1] + self.packed_flows[flow]['size'] > self.max_total_ep_info or self.ep_total_infos[ep2] + self.packed_flows[flow]['size'] > self.max_total_ep_info:
                        # would exceed at least 1 of this pair's end point's maximum load by adding this flow, move to next pair
                        continue
                    else:
                        chosen_pair = pair
                        break

            if chosen_pair is None:
                # could not find end point pair with enough capacity to take flow
                raise Exception('Unable to find valid pair to assign flow {}: {} without exceeding ep total information load limit {} information units for this session. Decrease required load, change node_dist, or decrease flow sizes to help with packing. Current end point total information loads (information units):\n{}'.format(flow, self.packed_flows[flow], self.max_total_ep_info, self.ep_total_infos))

            # pack flow into this pair
            self.pair_current_total_info_dict[chosen_pair] = int(self.pair_current_total_info_dict[chosen_pair] + self.packed_flows[flow]['size'])
            self.pair_current_distance_from_target_info_dict[chosen_pair] = int(self.pair_current_distance_from_target_info_dict[chosen_pair] - self.packed_flows[flow]['size'])

            # updated packed flows dict
            pair = json.loads(chosen_pair)
            i = np.random.choice([0, 1]) # sampling since src-dst pair order should be even across all pairs
            src = pair[i]
            if i == 0:
                dst = pair[1]
            else:
                dst = pair[0]
            self.packed_flows[flow]['src'], self.packed_flows[flow]['dst'] = src, dst 
            self.ep_total_infos[src] += self.packed_flows[flow]['size']
            self.ep_total_infos[dst] += self.packed_flows[flow]['size']
            packer_bar.next()

        # shuffle flow order to maintain randomness
        shuffled_packed_flows = {}
        shuffled_keys = list(self.packed_flows.keys())
        random.shuffle(shuffled_keys)
        for shuffled_key in shuffled_keys:
            shuffled_packed_flows[shuffled_key] = self.packed_flows[shuffled_key]

        packer_bar.finish()

        return shuffled_packed_flows
            
    def _check_node_dist_valid_for_this_load(self):
        for idx in self.index_to_node.keys():
            endpoint_target_load_fraction_of_overall_load = sum(self.node_dist[idx, :])
            endpoint_target_load_rate = endpoint_target_load_fraction_of_overall_load * self.load_rate
            if endpoint_target_load_rate > self.network_load_config['ep_link_capacity']:
                # target load rate is invalid
                if not self.auto_node_dist_correction:
                    # user has not enabled TrafPy automatic node distribution correction
                    raise Exception('Your node distribution is invalid for your specified target load. Overall target network load rate: {} info units per unit time. Endpoint {} (node dist idx {}) target fraction of this overall load: {}. Therefore target load rate for this endpoint is {} info units per unit time, which is too high for this end point which has a maximum capacity of {} info units per unit time, therefore your specified node load distribution is invalid. Change your required src-dst target loads in node_dist, or decrease your specified overall load target, or set auto_node_dist_correction=True to make TrafPy automatically correct the node distribution for you by subtracting the excess load of the invalid endpoint and distribution this excess load amongst other nodes (i.e. as your requested network load tends to 1.0, all end point loads will also tend to 1.0).'.format(self.load_rate, self.index_to_node[idx], idx, endpoint_target_load_fraction_of_overall_load, endpoint_target_load_rate, self.network_load_config['ep_link_capacity']))
                else:
                    print('auto_node_dist_correction set to True. Adjusting node distribution to make it valid...')
                    if self.print_data:
                        print('init node dist before correction:\n{}'.format(self.node_dist))
                    # self.auto_correction_spinner = Spinner('Adjusting node_dist ')
                    self.eps_at_capacity = {ep: False for ep in self.ep_total_infos.keys()}
                    invalid_ep_found = True
                    while invalid_ep_found:
                        invalid_ep_found = self._auto_correct_node_dist()
                    break

    def _auto_correct_node_dist(self):
        max_ep_load_frac = self.network_load_config['ep_link_capacity'] / self.load_rate # max fraction of total network load rate that one end point can take
        excess_ep_load_rates = {ep: None for ep in self.ep_total_infos.keys()}
        invalid_ep_found = False 
        for idx in self.index_to_node.keys():
            endpoint_target_load_fraction_of_overall_load = sum(self.node_dist[idx, :])
            endpoint_target_load_rate = round(endpoint_target_load_fraction_of_overall_load * self.load_rate, 3)
            if self.print_data:
                print('ep {} target load rate: {}'.format(idx, endpoint_target_load_rate))
            if endpoint_target_load_rate > self.network_load_config['ep_link_capacity']:
                # too much load rate on this ep
                invalid_ep_found = True
                excess_ep_load_rates[self.index_to_node[idx]] = endpoint_target_load_rate - self.network_load_config['ep_link_capacity']
                self.eps_at_capacity[self.index_to_node[idx]] = True
                # make ep loads equal on all this ep's pairs such that max ep bandwidth requested
                for pair_idx in self.index_to_node.keys():
                    if pair_idx != idx:
                        self.node_dist[idx, pair_idx] = (max_ep_load_frac / (self.num_nodes-1))
                        self.node_dist[pair_idx, idx] = (max_ep_load_frac / (self.num_nodes-1))
                # spread excess load evenly across other eps not already at capacity
                free_eps = []
                for ep in excess_ep_load_rates.keys():
                    if not self.eps_at_capacity[ep]:
                        free_eps.append(ep)
                if len(free_eps) == 0:
                    raise Exception('No free end points left to spread excess load across.')
                load_rate_to_spread_per_ep = excess_ep_load_rates[self.index_to_node[idx]] / len(free_eps)
                frac_load_rate_to_spread_per_ep = load_rate_to_spread_per_ep / self.load_rate
                frac_load_rate_to_spread_per_ep_pair = frac_load_rate_to_spread_per_ep / (self.num_nodes-1)
                for ep in free_eps:
                    for i in self.index_to_node.keys():
                        if i != self.node_to_index[ep] and not self.eps_at_capacity[self.index_to_node[i]]:
                            self.node_dist[self.node_to_index[ep], i] += frac_load_rate_to_spread_per_ep_pair
                            self.node_dist[i, self.node_to_index[ep]] += frac_load_rate_to_spread_per_ep_pair
                endpoint_target_load_fraction_of_overall_load = sum(self.node_dist[idx, :])
                endpoint_target_load_rate = endpoint_target_load_fraction_of_overall_load * self.load_rate
                self.reset() # update params
                if self.print_data:
                    print('updated node dist:\n{}'.format(self.node_dist))
            # self.auto_correction_spinner.next() 
        return invalid_ep_found















        

        












# FUNCTIONS

def get_flow_centric_demand_data_ep_load_rate(demand_data, ep, eps, method='all_eps'):
    '''
    If method=='all_eps', duration is time_last_flow_arrived-time_first_flow_arrived
    across all endpoints. If method=='per_ep', duration is time_last_flow_arrived-time_first_flow_arrived
    for this specific ep.
    '''
    ep_info = group_demand_data_into_ep_info(demand_data, eps)
    total_info = sum(ep_info[ep]['flow_size'])
    if method == 'per_ep':
        time_first_flow_arrived = min(ep_info[ep]['event_time'])
        time_last_flow_arrived = max(ep_info[ep]['event_time'])
    elif method == 'all_eps':
        time_first_flow_arrived = min(demand_data['event_time'])
        time_last_flow_arrived = max(demand_data['event_time'])
    duration = time_last_flow_arrived - time_first_flow_arrived
    load_rate = total_info / duration
    
    return load_rate

def get_flow_centric_demand_data_overall_load_rate(demand_data, bidirectional_links=True):
    '''
    If flow connections are bidirectional_links, 1 flow takes up 2 endpoint links (the
    source link and the destination link), therefore effecitvely takes up load rate
    2*flow_size*duration bandwidth. If not bidriectional, only takes up
    1*flow_size*duration since only occupies bandwidth for 1 of these links.

    If method == 'mean_per_ep', will calculate the total network load as being the mean
    average load on each endpoint link (i.e. sum info requests for each link ->
    find load of each link -> find mean of ep link loads)

    If method == 'mean_all_eps', will calculate the total network load as being
    the average load over all endpoint links (i.e. sum info requests for all links
    -> find overall load of network)
    '''
    info_arrived = get_flow_centric_demand_data_total_info_arrived(demand_data)
    first_event_time, last_event_time = get_first_last_flow_arrival_times(demand_data)
    duration = last_event_time - first_event_time

    if bidirectional_links:
        # 1 flow occupies 2 endpoint links therefore has 2*flow_size load
        load_rate = 2*info_arrived/duration
    else:
        load_rate = info_arrived/duration

    return load_rate



def group_demand_data_into_ep_info(demand_data, eps):
    nested_dict = lambda: defaultdict(nested_dict)
    ep_info = nested_dict()
    added_flow = {flow_id: False for flow_id in demand_data['flow_id']}
    for ep in eps:
        ep_info[ep]['flow_size'] = []
        ep_info[ep]['event_time'] = []
        ep_info[ep]['demand_data_idx'] = []
        ep_info[ep]['flow_id'] = []
        ep_info[ep]['establish'] = []
        ep_info[ep]['index'] = []
        ep_info[ep]['sn'] = []
        ep_info[ep]['dn'] = []
    # group demand data by ep
    for idx in range(len(demand_data['flow_id'])):
        if not added_flow[demand_data['flow_id'][idx]]: 
            # not yet added this flow
            ep_info[demand_data['sn'][idx]]['flow_size'].append(demand_data['flow_size'][idx])
            ep_info[demand_data['dn'][idx]]['flow_size'].append(demand_data['flow_size'][idx])
            ep_info[demand_data['sn'][idx]]['event_time'].append(demand_data['event_time'][idx])
            ep_info[demand_data['dn'][idx]]['event_time'].append(demand_data['event_time'][idx])
            ep_info[demand_data['sn'][idx]]['demand_data_idx'].append(idx)
            ep_info[demand_data['dn'][idx]]['demand_data_idx'].append(idx)
            ep_info[demand_data['sn'][idx]]['flow_id'].append(demand_data['flow_id'][idx])
            ep_info[demand_data['dn'][idx]]['flow_id'].append(demand_data['flow_id'][idx])
            ep_info[demand_data['sn'][idx]]['establish'].append(demand_data['establish'][idx])
            ep_info[demand_data['dn'][idx]]['establish'].append(demand_data['establish'][idx])
            ep_info[demand_data['sn'][idx]]['index'].append(demand_data['index'][idx])
            ep_info[demand_data['dn'][idx]]['index'].append(demand_data['index'][idx])
            ep_info[demand_data['sn'][idx]]['sn'].append(demand_data['sn'][idx])
            ep_info[demand_data['sn'][idx]]['dn'].append(demand_data['dn'][idx])
            ep_info[demand_data['dn'][idx]]['sn'].append(demand_data['sn'][idx])
            ep_info[demand_data['dn'][idx]]['dn'].append(demand_data['dn'][idx])
        else:
            # already added this flow
            pass

    return ep_info


def get_flow_centric_demand_data_total_info_arrived(demand_data): 
    info_arrived = 0
    # print('flow size {} {}'.format(type(demand_data['flow_size']), type(demand_data['flow_size'][0])))
    for flow_size in demand_data['flow_size']:
        if flow_size > 0:
            info_arrived += flow_size
        else:
            pass
    
    return info_arrived
            
def get_first_last_flow_arrival_times(demand_data):
    arrival_times = []
    for idx in range(len(demand_data['event_time'])):
        if demand_data['flow_size'][idx] > 0 and demand_data['sn'][idx] != demand_data['dn'][idx]:
            arrival_times.append(demand_data['event_time'][idx])
        else:
            pass
    if len(arrival_times) == 0:
        raise Exception('Could not find first event establish request with size > 0.. This occurs because either demand_data given does not contain any events, or because all events have had to be dropped to try get below your specified target load. Try increasing the target load or increasing the granularity of load per demand (by e.g. decreasing demand sizes, increasing total number of demands, etc.) when you generate your demand data so that this function can more easily hit your desired load target.')

    time_first_flow_arrived = min(arrival_times)
    time_last_flow_arrived = max(arrival_times)
    
    return time_first_flow_arrived, time_last_flow_arrived




def duplicate_demands_in_demand_data_dict(demand_data, method='all_eps', **kwargs):
    '''
    If method == 'all_eps', will duplicate all demands by adding final event time
    over all endpoints to each event time

    if method == 'per_ep', will duplicate all demands by adding final even time
    for each endpoint's final event time

    '''
    copy_demand_data = copy.deepcopy(demand_data) 

    # ensure values of dict are lists
    for key, value in copy_demand_data.items():
        copy_demand_data[key] = list(value)

    if method == 'all_eps':
        # final_event_time = max(demand_data['event_time'])
        num_demands = len(demand_data['flow_id'])
        final_event_time = max(demand_data['event_time'])
        first_event_time = min(demand_data['event_time'])
        duration = final_event_time - first_event_time
        for idx in range(len(demand_data['flow_id'])):
            copy_demand_data['flow_id'].append('flow_{}'.format(int(idx+num_demands)))
            copy_demand_data['sn'].append(demand_data['sn'][idx])
            copy_demand_data['dn'].append(demand_data['dn'][idx])
            copy_demand_data['flow_size'].append(demand_data['flow_size'][idx])
            # copy_demand_data['event_time'].append(final_event_time + demand_data['event_time'][idx])
            copy_demand_data['event_time'].append(duration + demand_data['event_time'][idx])
            copy_demand_data['establish'].append(demand_data['establish'][idx])
            copy_demand_data['index'].append(demand_data['index'][idx] + idx)

    elif method == 'per_ep':
        original_ep_info = group_demand_data_into_ep_info(copy_demand_data, eps=kwargs['eps'])
        # idx_iterator = iter(range(num_demands))
        duplicated_flows = {flow_id: False for flow_id in copy_demand_data['flow_id']}
        print('Flows before duplication: {}'.format(len(copy_demand_data['flow_id'])))
        idx_iterator = iter(range(len(copy_demand_data['flow_id'])))
        for ep in kwargs['eps']:
            # ep_info = group_demand_data_into_ep_info(copy_demand_data, eps=kwargs['eps'])
            num_demands = len(original_ep_info[ep]['flow_id'])

            first_event_time = min(original_ep_info[ep]['event_time'])
            final_event_time = max(original_ep_info[ep]['event_time'])
            duration = final_event_time - first_event_time

            # DEBUG 
            total_info = sum(original_ep_info[ep]['flow_size'])
            num_flows = len(original_ep_info[ep]['flow_id'])
            load = total_info / (final_event_time - first_event_time)
            print('Init {} duration: {} total info: {} | load: {} | flows: {}'.format(ep, duration, total_info, load, num_flows))

            for ep_flow_idx in range(len(original_ep_info[ep]['flow_id'])):
                flow_id = original_ep_info[ep]['flow_id'][ep_flow_idx]
                if not duplicated_flows[flow_id]:
                    duplicated_flows[flow_id] = True
                    # not yet duplicated this flow
                    # i = find_index_of_int_in_str(flow_id)
                    # idx = int(flow_id[i:])
                    idx = next(idx_iterator)

                    # copy_demand_data['flow_id'].append('flow_{}'.format(int(idx+num_demands)))
                    copy_demand_data['flow_id'].append('flow_{}'.format(int(idx+len(demand_data['flow_id']))))
                    copy_demand_data['sn'].append(original_ep_info[ep]['sn'][ep_flow_idx])
                    copy_demand_data['dn'].append(original_ep_info[ep]['dn'][ep_flow_idx])
                    copy_demand_data['flow_size'].append(original_ep_info[ep]['flow_size'][ep_flow_idx])
                    copy_demand_data['event_time'].append(duration + original_ep_info[ep]['event_time'][ep_flow_idx])
                    copy_demand_data['establish'].append(original_ep_info[ep]['establish'][ep_flow_idx])
                    copy_demand_data['index'].append(original_ep_info[ep]['index'][ep_flow_idx] + idx)
                else:
                    # already duplicated this flow in copy_demand_data
                    pass
            # DEBUG
            _ep_info = group_demand_data_into_ep_info(copy_demand_data, eps=kwargs['eps'])
            final_event_time = max(_ep_info[ep]['event_time'])
            first_event_time = min(_ep_info[ep]['event_time'])
            total_info = sum(_ep_info[ep]['flow_size'])
            load = total_info / (final_event_time - first_event_time)
            num_flows = len(_ep_info[ep]['flow_id'])
            print('Adjusted {} duration: {} total info: {} | load: {} | flows: {}'.format(ep, duration, total_info, load, num_flows))
        print('Flows after duplication: {}'.format(len(copy_demand_data['flow_id'])))

    # ensure values of dict are lists
    for key, value in copy_demand_data.items():
        copy_demand_data[key] = list(value)



    return copy_demand_data








