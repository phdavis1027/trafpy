from trafpy.generator.src import graphs
import numpy as np
import copy
import pickle
import bz2
import networkx as nx
import sys
import os
import matplotlib.pyplot as plt
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
import matplotlib.animation as animation
import cv2
import io
import time


class DCN:
    def __init__(self, 
                 Network, 
                 Demand, 
                 Scheduler, 
                 slot_size, 
                 max_flows=None, 
                 max_time=None):

        # initialise DCN environment characteristics
        self.network = Network
        self.demand = Demand
        self.scheduler = Scheduler
        self.slot_size = slot_size
        self.max_flows = max_flows # max number of flows per queue
        self.max_time = max_time



    def get_fat_tree_positions(self, net, width_scale=500, height_scale=10):
        pos = {}

        node_type_dict = self.get_node_type_dict(net, net.graph['node_labels'])
        node_types = list(node_type_dict.keys())
        
        heights = {} # dict for heigh separation between fat tree layers
        widths = {} # dict for width separation between nodes within layers
        h = iter([1, 2, 3, 4]) # server, edge, agg, core heights
        for node_type in node_types: 
            heights[node_type] = next(h)
            widths[node_type] = 1/(len(node_type_dict[node_type])+1)
            idx = 0
            for node in node_type_dict[node_type]:
                pos[node] = ((idx+1)*widths[node_type]*width_scale,heights[node_type]*height_scale)
                idx += 1

        return pos
        


    def init_network_node_positions(self, net):
        if net.graph['topology_type'] == 'fat_tree':
            pos = self.get_fat_tree_positions(net)


        else:
            pos = nx.nx_agraph.graphviz_layout(net, prog='neato')
        
        return pos

    def reset(self, pickled_demand_path=None, return_obs=True):
        '''
        Resets DCN simulation environment
        '''
        self.curr_step = 0
        self.curr_time = 0

        if pickled_demand_path is not None:
            # load a previously saved demand object
            filehandler = open(pickled_demand_path, 'rb')
            self.demand = pickle.load(filehandler)
        self.slots_dict = self.demand.get_slots_dict(self.slot_size)
        self.check_if_pairs_valid(self.slots_dict)

        self.num_endpoints = int(len(self.network.graph['endpoints']))

        self.net_node_positions = self.init_network_node_positions(copy.deepcopy(self.network))
        self.animation_images = []

        self.action = {'chosen_flows': []} # init

        self.channel_names = self.network.graph['channel_names'] 

        if self.demand.job_centric:
            self.network.graph['queued_jobs'] = [] # init list of curr queued jobs in network
            self.arrived_jobs = {} # use dict so can hash to make searching faster
            self.arrived_job_dicts = []
            self.completed_jobs = []
            self.dropped_jobs = []
            self.arrived_control_deps = [] # list of control dependencies
            self.arrived_control_deps_that_were_flows = []
            self.running_ops = {}
        else:
            pass
        self.connected_flows = []
        self.arrived_flows = {}
        self.arrived_flow_dicts = [] # use for calculating throughput at end
        self.completed_flows = []
        self.dropped_flows = []


        self.network = self.init_virtual_queues(self.network)
        self.queue_evolution_dict = self.init_queue_evolution(self.network)

        if return_obs:
            return self.next_observation()
        else:
            return None

    def check_if_pairs_valid(self, slots_dict):
        '''
        Since the network and the demand for a simulation are created separately,
        an easy mistake to fall into is to name the network nodes in the network
        differently from the src-dst pairs in the demand. This can lead to 
        infinite loops since the flows never get added to appropriate queues!
        This function loops through all the src-dst pairs in the first slot
        of the slots dict to try to catch this error before the simulation is
        ran
        '''
        slot = slots_dict[0]
        for event in slot['new_event_dicts']:
            if self.demand.job_centric:
                for f in event['flow_dicts']:
                    if f['src'] not in self.network.nodes or f['dst'] not in self.network.nodes:
                        sys.exit('ERROR: Demand src-dst pair names (e.g. {}-{}) different from \
                        network node names (e.g. {}). Rename one or the other to avoid errors!'.format(f['src'],f['dst'],list(self.network.nodes)[0]))
            else:
                if event['src'] not in self.network.nodes or event['dst'] not in self.network.nodes:
                    sys.exit('ERROR: Demand src-dst pair names (e.g. {}) different from \
                        network node names (e.g. {}). Rename one or the other to avoid errors!'.format(event['src'],list(self.network.nodes)[0]))

    
    def init_queue_evolution(self, Graph):
        q_dict = {src: 
                    {dst: 
                        {'times': [0],
                         'queue_lengths': [0]}
                        for dst in [dst for dst in Graph.graph['endpoints'] if dst != src]}
                    for src in Graph.graph['endpoints']} 

        return q_dict
    
    def calc_queue_length(self, src, dst):
        '''
        Calc queue length in bytes at a given src-dst queue
        '''
        queue = self.network.nodes[src][dst]
        num_flows = len(queue['queued_flows'])
        
        queue_length = 0
        for flow_idx in range(num_flows):
            flow_dict = queue['queued_flows'][flow_idx]
            if flow_dict['packets'] is None:
                # scheduler agent not yet chosen this flow therefore don't 
                # know chosen packet sizes, so size == original flow size
                queued_flow_bytes = flow_dict['size']
            else:
                # scheduler agent has since chosen flow, use packets left
                # to get queue length
                queued_flow_bytes = sum(flow_dict['packets'])
            queue_length += queued_flow_bytes

        return queue_length
    
    def update_queue_evolution(self):
        q_dict = self.queue_evolution_dict
        time = self.curr_time
        
        for src in self.network.graph['endpoints']:
            for dst in self.network.graph['endpoints']:
                if dst != src:
                    length = self.calc_queue_length(src, dst)
                    q_dict[src][dst]['times'].append(time)
                    q_dict[src][dst]['queue_lengths'].append(length)
                else:
                    # can't have src == dst
                    pass
    
    def init_virtual_queues(self, Graph):
        queues_per_ep = self.num_endpoints-1
      
        # initialise queues at each endpoint as node attributes
        attrs = {ep: 
                    {dst: 
                          {'queued_flows': [],
                           'completion_times': []}
                          for dst in [dst for dst in Graph.graph['endpoints'] if dst != ep]} 
                    for ep in Graph.graph['endpoints']}

        # add these queues/attributes to endpoint nodes in graph
        nx.set_node_attributes(Graph, attrs)

        return Graph
    
    def add_flow_to_queue(self, flow_dict):
        '''
        Adds a new flow to the appropriate src-dst virtual queue in the 
        simulator's network. Also updates arrived flows record
        '''
        # add to arrived flows list
        self.register_arrived_flow(flow_dict)
        
        src = flow_dict['src']
        dst = flow_dict['dst']
        
        # check if enough space to add flow to queue
        if self.max_flows is None:
            # no limit on number of flows in queue
            add_flow = True
        else:
            # check if adding flow would exceed queue limit
            curr_num_flows = len(self.network.nodes[src][dst]['queued_flows'])
            if curr_num_flows == self.max_flows:
                # queue full, cannot add flow to queue
                add_flow = False
            else:
                # there is enough space to add flow to queue
                add_flow = True
        
        if add_flow:
            # enough space in queue, add flow
            self.network.nodes[src][dst]['queued_flows'].append(flow_dict)
            self.network.nodes[src][dst]['completion_times'].append(None)
        else:
            # no space in queue, must drop flow
            self.dropped_flows.append(flow_dict)
            if self.demand.job_centric:
                for job_dict in self.network.graph['queued_jobs']:
                    if job_dict['job_id'] == flow_dict['job_id']:
                        # drop job
                        self.dropped_jobs.append(job_dict)
                        self.remove_job_from_queue(job_dict)
                        break
                        

    def add_job_to_queue(self, job_dict, print_times=False):
        '''
        Adds a new job with its respective flows to the appropriate
        src-dst virtual queue in the simulator's network. Aslo updates
        arrived flows record
        '''
        time_started_adding = time.time()
        # add to arrived jobs list
        self.arrived_jobs[job_dict['job_id']] = 'present' # record arrived job as being present in queue
        self.arrived_job_dicts.append(job_dict)
        self.network.graph['queued_jobs'].append(job_dict)
   
        # to ensure all child flows of completed 'flows' are updated, need to wait
        # until have gone through and queued all flows in new job graph to update
        # any flows that are completed immediately (e.g. ctrl deps coming from source)
        flows_to_complete = []
        
        start = time.time()
        for flow_dict in job_dict['flow_dicts']:
            if int(flow_dict['parent_op_run_time']) == 0:
                # parent op instantly completed
                flow_dict['time_parent_op_started'] = 0
            else:
                pass
            if self.arrived_jobs[job_dict['job_id']] == 'present':
                #if flow_dict['src'] == flow_dict['dst']:
                #    # src == dst therefore never becomes a flow
                #    flow_dict['can_schedule'] = 1 # need to change, see bottom of this method Note
                #    flow_dict['time_arrived'] = self.curr_time
                #if flow_dict['src'] != flow_dict['dst'] and int(flow_dict['size']) == 0:
                if int(flow_dict['size']) == 0:
                    # is a control dependency or src==dst therefore never becomes a flow therefore treat as control dep
                    self.arrived_control_deps.append(flow_dict)
                    if flow_dict['src'] == flow_dict['dst'] and flow_dict['dependency_type'] == 'data_dep':
                        self.arrived_control_deps_that_were_flows.append(flow_dict)
                    if int(flow_dict['parent_op_run_time']) == 0 and len(flow_dict['completed_parent_deps']) == len(flow_dict['parent_deps']):
                        # control dependency satisfied immediately
                        flow_dict['time_arrived'] = self.curr_time
                        flow_dict['can_schedule'] = 1
                        flows_to_complete.append(flow_dict)
                elif len(flow_dict['parent_deps']) == 0 and flow_dict['src'] != flow_dict['dst'] and flow_dict['size'] != 0:
                    # flow with no parent dependencies, count as having arrived immediately
                    flow_dict['time_arrived'] = job_dict['time_arrived']
                    self.add_flow_to_queue(flow_dict)
                else:
                    # flow with parent dependencies, dont count as having arrived immediately
                    self.add_flow_to_queue(flow_dict)
            else:
                # job was dropped due to full flow queue
                break
        end = time.time()
        if print_times:
            print('Time to add job to queue: {}'.format(end-start))
        
        # go back through and register any immediately completed 'flows'
        start = time.time()
        for f in flows_to_complete:
            self.register_completed_flow(f, print_times=False)
        end = time.time()
        if print_times:
            print('Time to register immediately completed flows: {}'.format(end-start))

        time_finished_adding = time.time()
        if print_times:
            print('Total time to add job to queue & register completed flows: {}'.format(time_finished_adding-time_started_adding))
       
       # # go through added flows and check for size == 0 'flows' that can sched
       # for flow_dict in job_dict['flow_dicts']:
       #     '''
       #     NOTE: 'Flows' with src == dst or size == 0 (data deps) never become flows. But,
       #     they still have prior dependencies in job graph. At some point, need
       #     to account for these dependencies by only counting can_schedule = 1 
       #     size 0 flows as having been completed. To do this, will also need
       #     to update child dependencies when register size 0 flow as complete or
       #     will get stuck in infinite scheduling loop since child dependencies
       #     will never know prev flow completed
       #     '''
       #     #if flow_dict['can_schedule'] == 1 and flow_dict['src'] == flow_dict['dst']:
       #     if flow_dict['src'] == flow_dict['dst']:
       #         flow_dict['time_arrived'] = job_dict['time_arrived']
       #         # For size == 0 'flows', count 'flow' as complete
       #         self.register_completed_flow(flow_dict)
            
        
        
        
        
        
        
            
            
    def update_curr_time(self, slot_dict):
        '''
        Updates current time of simulator using slot dict
        '''
        # update current time
        if slot_dict['ub_time'] > self.curr_time:
            # observation has a new up-to-date current time
            self.curr_time = slot_dict['ub_time']
        else:
            # observation does not have an up-to-date time
            self.curr_time += self.slot_size
            num_decimals = str(self.slot_size)[::-1].find('.')
            self.curr_time = round(self.curr_time,num_decimals)
    
    def update_running_op_dependencies(self, observation):
        '''
        Takes observation of current time slot and updates dependencies of any
        ops that are running
        '''
        # go through queued flows
        eps = self.network.graph['endpoints']
        for ep in eps:
            ep_queues = self.network.nodes[ep]
            for ep_queue in ep_queues.values():
                for flow_dict in ep_queue['queued_flows']:
                    if flow_dict['time_parent_op_started'] is not None:
                        if self.curr_time >= flow_dict['time_parent_op_started'] + flow_dict['parent_op_run_time']:
                            # parent op has finished running, can schedule flow
                            #op_id = flow_dict['job_id']+'_op_'+str(flow_dict['parent_op'])
                            op_id = flow_dict['job_id']+'_'+flow_dict['parent_op']
                            try:
                                del self.running_ops[op_id]
                            except KeyError:
                                # op has already previously been registered as completed
                                pass
                            if flow_dict['can_schedule'] == 0:
                                flow_dict['can_schedule'] = 1
                                flow_dict['time_arrived'] = self.curr_time
                                self.register_arrived_flow(flow_dict)
                            else:
                                # already registered as arrived
                                pass
                        else:
                            # child op not yet finished, cannot schedule
                            pass
                    else:
                        # can already schedule or child op not started therefore dont need to consider
                        pass

        # go through queued control dependencies
        for dep in self.arrived_control_deps:
            if dep['time_parent_op_started'] is not None and dep['time_completed'] is None:
                # dep child op has begun and dep has not been registered as completed
                if self.curr_time >= dep['time_parent_op_started'] + dep['parent_op_run_time']:
                    # child op has finished running, dependency has been completed
                    #op_id = flow_dict['job_id']+'_op_'+str(flow_dict['parent_op'])
                    op_id = dep['job_id']+'_'+dep['parent_op']
                    try:
                        del self.running_ops[op_id]
                    except KeyError:
                        # op has already previously been registered as completed
                        pass
                    dep['time_completed'] = self.curr_time
                    dep['can_schedule'] = 1
                    self.register_completed_flow(dep)
                else:
                    # parent op not yet finished
                    pass
            else:
                # parent op not yet started
                pass

        observation['network'] = self.network

        return observation

                        
    def add_flows_to_queues(self, observation):
        '''
        Takes observation of current time slot and updates virtual queues in
        network
        '''
        slot_dict = observation['slot_dict']

        if len(slot_dict['new_event_dicts']) == 0:
            # no new event(s)
            pass
        else:
            # new event(s)
            num_events = int(len(slot_dict['new_event_dicts']))
            for event in range(num_events):
                event_dict =    slot_dict['new_event_dicts'][event]
                if event_dict['establish'] == 0:
                    # event is a take down event, don't need to consider
                    pass
                elif event_dict['establish'] == 1:
                    if self.demand.job_centric:
                        self.add_job_to_queue(event_dict, print_times=False)
                    else:
                        self.add_flow_to_queue(event_dict)
        
        observation['network'] = self.network # update osbervation's network
        
        return observation
    
    def remove_job_from_queue(self, job_dict):
        idx = 0
        queued_jobs = copy.deepcopy(self.network.graph['queued_jobs'])

        eps = copy.deepcopy(self.network.graph['endpoints'])
        for ep in eps:
            ep_queues = self.network.nodes[ep]
            for ep_queue in ep_queues.values():
                for f in ep_queue['queued_flows']:
                    if f['job_id'] == job_dict['job_id']:
                        self.remove_flow_from_queue(flow_dict)
                    else:
                        # flow does not belong to job being removed
                        pass

        self.arrived_jobs[job_dict['job_id']] = 'removed' 
        #idx = 0
        #for job in self.network.graph['queued_jobs']:
        #    if job['job_id'] == job_dict['job_id']:
        #        del self.network.graph['queued_jobs'][idx]
        #        break
        #    else:
        #        # not this job, move to next job
        #        idx += 1
            
        
    
    def remove_flow_from_queue(self, flow_dict):
        if flow_dict['src'] == flow_dict['dst']:
            pass
        else:
            sn = flow_dict['src']
            dn = flow_dict['dst']
            queued_flows = self.network.nodes[sn][dn]['queued_flows']
            idx = self.find_flow_idx(flow_dict, queued_flows)
            del self.network.nodes[sn][dn]['queued_flows'][idx]
            del self.network.nodes[sn][dn]['completion_times'][idx]
    
    def register_completed_flow(self, flow_dict, print_times=False):
        '''
        Takes a completed flow, appends it to list of completed flows, records
        time at which it was completed, and removes it from queue. If 'flow' in
        fact never become a flow (i.e. had src == dst or was control dependency
        with size == 0), will update dependencies but won't append to completed
        flows etc.
        '''
      
        # record time at which flow was completed
        start = time.time()
        flow_dict['time_completed'] = copy.copy(self.curr_time)
        if flow_dict['size'] != 0 and flow_dict['src'] != flow_dict['dst']:
            # flow was an actual flow
            self.completed_flows.append(flow_dict)
            fct = flow_dict['time_completed'] - flow_dict['time_arrived']
        else:
            # 'flow' never actually became a flow (src == dst or control dependency)
            pass
        end = time.time()
        if print_times:
            print('\nTime to record time flow completed: {}'.format(end-start))
        
        start = time.time()
        f = copy.copy(flow_dict)
        if flow_dict['size'] != 0:
            # remove flow from queue
            self.remove_flow_from_queue(flow_dict)
        else:
            # never became flow 
            pass
        end = time.time()
        if print_times:
            print('Time to remove flow from global queue: {}'.format(end-start))
        
        start = time.time()
        if self.demand.job_centric:
            # make any necessary job completion & job dependency changes
            self.update_completed_flow_job(f)
        end = time.time()
        if print_times:
            print('Time to record any job completions and job dependency changes: {}'.format(end-start))


    def register_arrived_flow(self, flow_dict):
        # register
        if flow_dict['can_schedule'] == 1:
            # flow is ready to be scheduled therefore can count as arrived
            if self.demand.job_centric:
                arrival_id = flow_dict['job_id']+'_'+flow_dict['flow_id']
            else:
                arrival_id = flow_dict['flow_id']
            try:
                _ = self.arrived_flows[arrival_id]
                # flow already counted as arrived
            except KeyError:
                # flow not yet counted as arrived
                if flow_dict['src'] != flow_dict['dst'] and flow_dict['size'] != 0:
                    if flow_dict['time_arrived'] is None:
                        # record time flow arrived
                        flow_dict['time_arrived'] = self.curr_time
                    else:
                        # already recorded time of arrival
                        pass
                    self.arrived_flows[arrival_id] = 'present'
                    self.arrived_flow_dicts.append(flow_dict)
                else:
                    # 'flow' never actually becomes flow (is ctrl dependency or src==dst)
                    pass
            #flow_arrived, _ = self.check_flow_present(flow_dict, self.arrived_flows)
            #if flow_arrived:
            #    # flow already counted as arrived
            #    pass
            #else:
            #    # flow not yet counted as arrived
            #    if flow_dict['src'] != flow_dict['dst'] and flow_dict['size'] != 0:
            #        if flow_dict['time_arrived'] is None:
            #            # record time flow arrived
            #            flow_dict['time_arrived'] = self.curr_time
            #        else:
            #            # already recorded time of arrival
            #            pass
            #        self.arrived_flows.append(flow_dict)
            #    else:
            #        # 'flow' never actually becomes flow (is ctrl dependency or src==dst)
            #        pass
        else:
            # can't yet schedule therefore don't count as arrived
            pass
        


    def update_flow_packets(self, flow_dict):
        '''
        Takes flow dict that has been schedueled to be activated for curr
        time slot and removes corresponding number of packets flow in queue
        '''
        packet_size = flow_dict['packets'][0] 
        path_links = self.get_path_edges(flow_dict['path'])
        link_bws = []
        for link in path_links:
            link_bws.append(self.network[link[0]][link[1]]['max_channel_capacity'])
        lowest_bw = min(link_bws)
        size_per_slot = lowest_bw/(1/self.slot_size)
        packets_per_slot = int(size_per_slot / packet_size) # round down 
        
        sn = flow_dict['src']
        dn = flow_dict['dst']
        queued_flows = self.network.nodes[sn][dn]['queued_flows']
        idx = self.find_flow_idx(flow_dict, queued_flows)
        queued_flows[idx]['packets'] = queued_flows[idx]['packets'][packets_per_slot:]
        
        updated_flow = copy.copy(queued_flows[idx])
        if len(updated_flow['packets']) == 0:
            # all packets transported, flow completed
            self.register_completed_flow(updated_flow)
                
        
        return flow_dict

    def get_current_queue_states(self):
        '''
        Returns list of all queues in network
        '''
        queues = []
        eps = self.network.graph['endpoints']
        for ep in eps:
            ep_queues = self.network.nodes[ep]
            for ep_queue in ep_queues.values():
                if len(ep_queue['queued_flows'])!=0:
                    queues.append(ep_queue)
        return queues
                

    def next_observation(self):
        '''
        Compiles simulator data and returns observation
        '''
        try:
            observation = {'slot_dict': self.slots_dict[self.curr_step],
                           'network': copy.deepcopy(self.network)}
            self.update_curr_time(observation['slot_dict'])
            # add any new events (flows or jobs) to queues
            observation = self.add_flows_to_queues(observation)
        except KeyError:
            # curr step exceeded slots dict indices, no new flows/jobs arriving
            slot_keys = list(self.slots_dict.keys())
            observation = {'slot_dict': self.slots_dict[slot_keys[-1]],
                           'network': copy.deepcopy(self.network)}
            self.update_curr_time(observation['slot_dict'])
        
        # update any dependencies of running ops
        if self.demand.job_centric:
            observation = self.update_running_op_dependencies(observation)

            
        return observation

    def check_num_channels_used(self, graph, edge):
        '''
        Checks number of channels currently in use on given edge in graph
        '''
        num_channels = graph.graph['num_channels_per_link']
        num_channels_used = 0

        for channel in self.channel_names:
            channel_used = self.check_if_channel_used(graph, [edge], channel)
            if channel_used:
                num_channels_used += 1
            else:
                pass
        
        return num_channels_used, num_channels


    def get_node_type_dict(self, network, node_types=[]):
        '''
        Gets dict where keys are node types and values are list of nodes for
        each node type in graph
        '''
        network_nodes = []
        for network_node in network.nodes:
            network_nodes.append(network_node)
        network_nodes_dict = {node_type: [] for node_type in node_types}
        for n in network_nodes:
            for node_type in node_types:
                if node_type in n:
                    network_nodes_dict[node_type].append(n)
                else:
                    # not this node type
                    pass
        
        return network_nodes_dict



    def draw_network_state(self,
                           draw_flows=True,
                           draw_ops=True,
                           draw_node_labels=False,
                           ep_label='server',
                           appended_node_size=300, 
                           network_node_size=2000,
                           appended_node_x_spacing=5,
                           appended_node_y_spacing=0.75,
                           font_size=15, 
                           linewidths=1, 
                           fig_scale=2):
        '''
        Draws network state as matplotlib figure
        '''
    
        network = copy.deepcopy(self.network)

        fig = plt.figure(figsize=[15*fig_scale,15*fig_scale])

        # add nodes and edges
        pos = {}
        flows = []
        network_nodes = []
        ops = []
        network_nodes_dict = self.get_node_type_dict(network, self.network.graph['node_labels'])
        for nodes in list(network_nodes_dict.values()):
            for network_node in nodes:
                pos[network_node] = self.net_node_positions[network_node]

        eps = network.graph['endpoints']
        for ep in eps:
            ep_queues = network.nodes[ep]
            y_offset = -appended_node_y_spacing
            for ep_queue in ep_queues.values():
                for flow in ep_queue['queued_flows']:
                    f_id = str(flow['job_id']+'_'+flow['flow_id'])
                    network.add_node(f_id)
                    network.add_edge(f_id, flow['src'], type='queue_link')
                    flows.append(f_id)
                    pos[f_id] = (list(pos[flow['src']])[0]+appended_node_x_spacing, list(pos[flow['src']])[1]+y_offset)
                    y_offset-=appended_node_y_spacing

        for ep in eps:
            y_offset = -appended_node_y_spacing
            for op in self.running_ops.keys():
                op_machine = self.running_ops[op]
                if ep == op_machine:
                    network.add_node(op)
                    network.add_edge(op, op_machine, type='op_link')
                    ops.append(op)
                    pos[op] = (list(pos[op_machine])[0]-appended_node_x_spacing, list(pos[op_machine])[1]+y_offset)
                    y_offset-=appended_node_y_spacing
                else:
                    # op not placed on this end point machine
                    pass

        # find edges
        fibre_links = []
        queue_links = []
        op_links = []
        for edge in network.edges:
            if 'channels' in network[edge[0]][edge[1]].keys():
                # edge is a fibre link
                fibre_links.append(edge)
            elif network[edge[0]][edge[1]]['type'] == 'queue_link':
                # edge is a queue link
                queue_links.append(edge)
            elif network[edge[0]][edge[1]]['type'] == 'op_link':
                # edge is a queue link
                op_links.append(edge)
            else:
                sys.exit('Link type not recognised.')

        # network nodes
        node_colours = iter(['#36a0c7', '#e8b017', '#6115a3', '#160e63']) # server, edge, agg, core
        for node_type in self.network.graph['node_labels']:
            nx.draw_networkx_nodes(network, 
                                   pos, 
                                   nodelist=network_nodes_dict[node_type],
                                   node_size=network_node_size, 
                                   node_color=next(node_colours), 
                                   linewidths=linewidths, 
                                   label=node_type)

        if draw_flows:
            # flows
            nx.draw_networkx_nodes(network, 
                                   pos, 
                                   nodelist=flows,
                                   node_size=appended_node_size, 
                                   node_color='#bd3937', 
                                   linewidths=linewidths, 
                                   label='Queued flow')
            # queue links
            nx.draw_networkx_edges(network, 
                                   pos,
                                   edgelist=queue_links,
                                   edge_color='#bd3937',
                                   alpha=0.5,
                                   width=0.5,
                                   style='dashed',
                                   label='Queue link')

        if draw_ops and self.demand.job_centric:
            # ops
            nx.draw_networkx_nodes(network, 
                                   pos, 
                                   nodelist=ops,
                                   node_size=appended_node_size, 
                                   node_color='#1e9116', 
                                   linewidths=linewidths, 
                                   label='Running op')
            # op links
            nx.draw_networkx_edges(network, 
                                   pos,
                                   edgelist=op_links,
                                   edge_color='#1e9116',
                                   alpha=0.5,
                                   width=0.5,
                                   style='dashed',
                                   label='Op link')




        # fibre links
        nx.draw_networkx_edges(network, 
                               pos,
                               edgelist=fibre_links,
                               edge_color='k',
                               width=3,
                               label='Fibre link')

        if draw_node_labels:
            # nodes
            nx.draw_networkx_labels(network, 
                                    pos, 
                                    font_size=font_size, 
                                    font_color='k', 
                                    font_family='sans-serif', 
                                    font_weight='normal', 
                                    alpha=1.0)

        plt.legend(labelspacing=2.5)
    
        return fig, network, pos


    def render_network(self, action=None, fig_scale=1):
        '''
        Renders network state as matplotlib figure and, if specified, renders
        chosen action(s) (lightpaths) on top of figure
        '''
        fig, network, pos = self.draw_network_state(draw_flows=True,
                                                    draw_ops=True, 
                                                    fig_scale=fig_scale)
        
        if action is not None:
            # init fibre link labels
            fibre_link_labels = {}
            for edge in network.edges:
                if 'flow' in edge[0] or 'flow' in edge[1] or '_op_' in edge[0] or '_op_' in edge[1]:
                    # edge is not a fibre, dont need to label
                    pass
                else:
                    # fibre not yet added
                    fibre_link_labels[(edge[0], edge[1])] = 0

            # render selected actions/chosen lightpaths in network
            active_lightpath_edges = []
            for flow in action['chosen_flows']:
                path_edges = self.get_path_edges(flow['path'])
                f_id = str(flow['job_id']+'_'+flow['flow_id'])
                queue_link = [f_id, flow['src']]
                path_edges.append(queue_link)
                for edge in path_edges:  
                    active_lightpath_edges.append(edge)
                    if '_flow_' in edge[0] or '_flow_' in edge[1] or '_op_' in edge[0] or '_op_' in edge[1]:
                        # edge is not a fibre, dont need to label
                        pass
                    else:
                        # edge is fibre, label with number of active lightpaths
                        try:
                            fibre_link_labels[(edge[0], edge[1])] += 1
                        except KeyError:
                            fibre_link_labels[(edge[1], edge[0])] += 1

            # format fibre link labels
            for link in fibre_link_labels.keys():
                num_channels_used = fibre_link_labels[link]
                fibre_label = '{}/{}'.format(str(num_channels_used),self.network.graph['num_channels_per_link']) 
                fibre_link_labels[link] = fibre_label

            # lightpaths
            nx.draw_networkx_edges(network, 
                                   pos,
                                   edgelist=active_lightpath_edges,
                                   edge_color='#e80e0e',
                                   alpha=0.5,
                                   width=15,
                                   label='Active lightpath')
            
            # lightpath labels
            nx.draw_networkx_edge_labels(network,
                                         pos,
                                         edge_labels=fibre_link_labels,
                                         font_size=15,
                                         font_color='k',
                                         font_family='sans-serif',
                                         font_weigh='normal',
                                         alpha=1.0)


        else:
            # no action given, just render network queue state(s)
            pass

        plt.title('Time: {}'.format(self.curr_time), fontdict={'fontsize': 100})
        
        return fig



    def conv_fig_to_image(self, fig):
        '''
        Takes matplotlib figure and converts into numpy array of RGB pixel values
        '''
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=self.dpi)
        buf.seek(0)
        img_arr = np.frombuffer(buf.getvalue(), dtype=np.uint8)
        buf.close()
        img = cv2.imdecode(img_arr,1)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        return img

    
    def render(self, action=None, dpi=300, fig_scale=1):
        '''
        Renders current network state for final animation at end of scheduling
        session. If action is None, will only render network queue state(s)
        rather than also rendering selected lightpaths.
        '''
        self.dpi = dpi
        fig = self.render_network(copy.deepcopy(action), fig_scale=fig_scale)
        self.animation_images.append(self.conv_fig_to_image(fig))
        plt.close() 
    
    def register_completed_job(self, job_dict):
        
        # record time at which job was completed
        job_dict['time_completed'] = copy.copy(self.curr_time)
        self.completed_jobs.append(job_dict)
        jct = job_dict['time_completed']-job_dict['time_arrived']
        
        # remove job from queue
        self.remove_job_from_queue(job_dict) 
    
    def update_completed_flow_job(self, completed_flow):
        '''
        Updates dependencies of other flows in job of completed flow.
        Checks if all flows in job of completed flow have been completed.
        If so, will update stat trackers & remove from tracking list
        '''
        # update any dependencies of flow in job
        self.update_job_flow_dependencies(completed_flow)

        # check if job completed
        job_id = completed_flow['job_id']
        job_flows_completed, job_ctrl_deps_completed = True, True
        eps = self.network.graph['endpoints']
        # check job flows
        for ep in eps:
            ep_queues = self.network.nodes[ep]
            for ep_queue in ep_queues.values():
                for f in ep_queue['queued_flows']:
                    if f['job_id'] == job_id and f['time_completed'] is None:
                        # job still has at least one uncompleted flow left
                        job_flows_completed = False
                        break

        # check job ctrl deps
        for dep in self.arrived_control_deps:
            if dep['job_id'] == job_id and dep['time_completed'] is None:
                # job still has at least one uncompleted control dependency left
                job_ctrl_deps_completed = False
                break

        if job_flows_completed == True and job_ctrl_deps_completed == True:
            # register completed job
            for job in self.network.graph['queued_jobs']:
                if job['job_id'] == job_id:
                    self.register_completed_job(job)
                    break
        else:
            # job not yet completed, has outstanding dependencies
            pass
                    
    #def check_if_flow_has_null_child(self, flow_dict):
    #    '''
    #    checks if flow has a null child
    #    '''
    #    child_deps = null_flow['child_deps']
    #    null_flow_dict = None

    #    eps = self.network.grap['endpoints']
    #    for child_dep in child_deps:
    #        for ep in eps:
    #            ep_queues = self.network.nodes[ep]
    #            for ep_queue in ep_queues.values():
    #                for flow_dict in ep_queue['queued_flows']:
    #                    if flow_dict['job_id'] == null_flow['job_id']:
    #                        # only consider if part of same job
    #                        if flow_dict['id'] == child_dep:
    #                            # child found
    #                            if flow_dict['size'] == 0:
    #                                null_flow_dict = flow_dict
    #                                return null_flow_dict
  
    #    return null_flow_dict

    #                
    #def update_null_flow_size_dependencies(self, null_flow):
    #    '''
    #    Goes through flows in job and updates any child dependencies of flows
    #    which can now be 'scheduled' but which have size = 0 (i.e. src== dst) 
    #    therefore never becomes flow.  
    #    '''
    #    child_deps = null_flow['child_deps']
    #    null_child = False
    #    flows_to_consider = [null_flow]
    #    null_flow_dict = null_flow

    #    while True:
    #        while null_flow_dict is not None:
    #            null_flow_dict = self.check_if_flow_has_null_child(null_flow_dict)
    #            flows_to_consider.append(self.check_if_flow_has_null_child(null_flow_dict))

    #        for flow in flows_to_consider:
    #            null_child, null_flow_dict = self.check_if_flow_has_null_child(flow)

            
    
    def update_job_flow_dependencies(self, completed_flow):
        '''
        Go through flows in job and update any flows that were waiting for 
        completed flow to arrive before being able to be scheduled
        '''
        completed_child_dependencies = completed_flow['child_deps']
        eps = self.network.graph['endpoints']
        
        for child_dep in completed_child_dependencies:
            # go through queued flows
            for ep in eps:
                ep_queues = self.network.nodes[ep]
                for ep_queue in ep_queues.values():
                    for flow_dict in ep_queue['queued_flows']:
                        if flow_dict['job_id'] == completed_flow['job_id']:
                            # only update if part of same job!
                            if flow_dict['flow_id'] == child_dep:
                                # child dependency found
                                flow_dict['completed_parent_deps'].append(completed_flow['flow_id'])
                                if len(flow_dict['completed_parent_deps']) == len(flow_dict['parent_deps']):
                                    # parent dependencies of child op have been completed
                                    if flow_dict['parent_op_run_time'] > 0 and flow_dict['time_parent_op_started'] == None:
                                        # child op of flow has non-zero run time and has not yet started
                                        flow_dict['time_parent_op_started'] = self.curr_time
                                        #op_id = flow_dict['job_id']+'_op_'+str(flow_dict['parent_op'])
                                        op_id = flow_dict['job_id']+'_'+flow_dict['parent_op']
                                        op_machine = flow_dict['src']
                                        self.running_ops[op_id] = op_machine
                                    else:
                                        # child op of flow has 0 run time, can schedule flow now
                                        flow_dict['can_schedule'] = 1
                                        flow_dict['time_arrived'] = self.curr_time
                                        self.register_arrived_flow(flow_dict)
                                else:
                                    # still can't schedule
                                    pass
                            else:
                                # flow is not a child dep of completed flow
                                pass
                        else:
                            # flow not part of same job as completed flow
                            pass
                          
            for child_dep in completed_child_dependencies:
                # go through arrived control dependencies
                for control_dep in self.arrived_control_deps:
                    if control_dep['job_id'] == completed_flow['job_id']:
                        # only update if part of same job!
                        if control_dep['flow_id'] == child_dep:
                            # child dependency found
                            control_dep['completed_parent_deps'].append(completed_flow['flow_id'])
                            if len(control_dep['completed_parent_deps']) == len(control_dep['parent_deps']):
                                # parent dependencies of child op have been completed
                                if control_dep['parent_op_run_time'] > 0 and control_dep['time_parent_op_started'] == None:
                                    # child op of control dep has non-zero run time and has not yet started
                                    control_dep['time_parent_op_started'] = self.curr_time
                                    control_dep['time_arrived'] = self.curr_time
                                    #op_id = control_dep['job_id']+'_op_'+str(flow_dict['parent_op'])
                                    op_id = control_dep['job_id']+'_'+control_dep['parent_op']
                                    op_machine = control_dep['src']
                                    self.running_ops[op_id] = op_machine
                                else:
                                    # child op of control dep has 0 run time, control dependency has been satisfied
                                    control_dep['can_schedule'] = 1
                                    control_dep['time_arrived'] = self.curr_time
                            else:
                                # still can't schedule
                                pass
                        else:
                            # dep is not a child dep of completed flow
                            pass
                    else:
                        # dep not part of same job as completed flow
                        pass
                    

    def update_flow_attrs(self, chosen_flows):
        for flow in chosen_flows:
            sn = flow['src']
            dn = flow['dst']
            queued_flows = self.network.nodes[sn][dn]['queued_flows']
            idx = self.find_flow_idx(flow, queued_flows)
            dated_flow = self.network.nodes[sn][dn]['queued_flows'][idx]
            if dated_flow['packets'] is None:
                # udpate flow packets and k shortest paths
                dated_flow['packets'] = flow['packets']
                dated_flow['k_shortest_paths'] = flow['k_shortest_paths']
            else:
                # agent updates already applied
                pass

    def take_action(self, action):
        # unpack chosen action
        chosen_flows = action['chosen_flows']
        
        # update any flow attrs in dcn network that have been updated by agent
        self.update_flow_attrs(chosen_flows)

        # establish chosen flows
        for chosen_flow in chosen_flows:
            #flow_established, _ = self.check_flow_present(chosen_flow, self.connected_flows)
            if len(chosen_flows) == 0:
                # no flows chosen
                pass
            #elif flow_established:
            elif self.check_flow_present(chosen_flow, self.connected_flows):
                # chosen flow already established, leave
                pass
            else:
                # chosen flow not already established, establish connection
                self.set_up_connection(chosen_flow)

        # take down replaced flows
        for prev_chosen_flow in self.connected_flows:
            #flow_established, _ = self.check_flow_present(prev_chosen_flow, chosen_flows)
            if self.connected_flows == 0:
                # no flows chosen previously
                pass
            #elif flow_established:
            elif self.check_flow_present(prev_chosen_flow, chosen_flows):
                # prev chosen flow has been re-chosen, leave
                pass
            else:
                # prev chosen flow not re-chosen, take down connection
                self.take_down_connection(prev_chosen_flow)

        #self.update_completed_flows(chosen_flows) 
        for flow in chosen_flows:
            self.update_flow_packets(flow)
        
        
        #self.update_completed_flows(chosen_flows) 

        self.update_queue_evolution()

        # all chosen flows established and any removed flows taken down
        # update connected_flows
        self.connected_flows = chosen_flows.copy()


    def step(self, action):
        '''
        Performs an action in the DCN simulation environment, moving simulator
        to next step
        '''
       
        self.action = action # save action
        
        self.take_action(action)

        self.curr_step += 1
        reward = self.calc_reward()
        done = self.check_if_done()
        info = None
        obs = self.next_observation()

        # queues = self.get_current_queue_states()
        # print('arrived flows:\n{}'.format(self.arrived_flow_dicts))
        # print('arrived flow ids:\n{}'.format(self.arrived_flows))
        # print('Num completed flows: {}/{}'.format(len(self.completed_flows), self.demand.num_flows))
        # print('Queues being given to scheduler:')
        # i = 0
        # for q in queues:
           # print('queue {}:\n{}'.format(i, q))
           # i+=1
        # print('Incomplete control deps:')
        # for c in self.arrived_control_deps:
           # if c['time_completed'] is None or c['time_parent_op_started'] is None:
               # print(c)
           # else:
               # pass

        return obs, reward, done, info
  
    
    def calc_num_queued_flows_num_full_queues(self):
        '''
        Calc num queued flows and full queues in network
        '''
        num_full_queues = 0
        num_queued_flows = 0
        
        eps = self.network.graph['endpoints']
        for ep in eps:
            ep_queues = self.network.nodes[ep]
            for ep_queue in ep_queues.values():
                num_flows_in_queue = len(ep_queue['queued_flows'])
                num_queued_flows += num_flows_in_queue
                if self.max_flows is not None:
                    if num_flows_in_queue == self.max_flows:
                        num_full_queues += 1
                    elif num_flows_in_queue > self.max_flows:
                        sys.exit('Error: Num queued flows exceeded max_flows')
                    else:
                        pass
                else:
                    # no max number of flows therefore no full queues
                    pass

        return num_queued_flows, num_full_queues


    def calc_reward(self):
        if self.max_flows is None:
            # no maximum number of flows per queue therefore no full queues
            num_queued_flows, _ = self.calc_num_queued_flows_num_full_queues()
            r = - (self.slot_size * num_queued_flows)
        else:
            num_queued_flows, num_full_queues = self.calc_num_queued_flows_num_full_queues()
            r = - (self.slot_size) * (num_queued_flows + num_full_queues)

        return r

    def plot_single_queue_evolution(self, src, dst, path_figure):
        '''
        Plot queue evolution (queue length vs time) for a given src-dst queue
        '''
        q_dict = self.queue_evolution_dict
        times = q_dict[src][dst]['times']
        lengths = q_dict[src][dst]['queue_lengths']
        
        fig = plt.figure()
        plt.style.use('ggplot')
        plt.plot(times, lengths)
        plt.xlabel('Times (a.u.)')
        plt.ylabel('{}-{} Q Length (a.u.)'.format(src,dst))
        plt.savefig(path_figure + '{}-{}_q_length.png'.format(src,dst))
        plt.close()

    def plot_all_queue_evolution(self, path_figure):
        '''
        Plots queue evolution for all queues in network
        '''
        q_dict = self.queue_evolution_dict
        srcs = list(q_dict.keys())
        for src in srcs:
            dsts = list(q_dict[src].keys())
            for dst in dsts:
                self.plot_single_queue_evolution(src,dst,path_figure)
    
    def plot_set_queue_evolution(self, srcs, dsts, path_figure):
        '''
        Plots queue evolution for set of src-dst pairs (given as lists)
        '''
        for idx in range(len(srcs)):
            src = srcs[idx]
            dst = dsts[idx]
            self.plot_single_queue_evolution(src,dst,path_figure)
            
    
    def save_rendered_animation(self, path_animation, fps=1, bitrate=1800, animation_name='anim'):
        if len(self.animation_images) > 0:
            # rendered images ready to be made into animation
            print('\nSaving scheduling session animation...')
            plt.close()
            fig = plt.figure()
            fig.patch.set_visible(False)
            ax = fig.gca()
            ax.axis('off')
            plt.box(False)
            images = []
            for im in self.animation_images:
                img = plt.imshow(im)
                images.append([img])
            ani = animation.ArtistAnimation(fig,
                                            images,
                                            interval=1000,
                                            repeat_delay=5000,
                                            blit=True)
            Writer = animation.writers['ffmpeg']
            writer = Writer(fps=fps, bitrate=bitrate) #codec=libx264
            ani.save(path_animation + animation_name + '.mp4', writer=writer, dpi=self.dpi)
            print('Animation saved.')
        else:
            print('No images were rendered during scheduling session, therefore\
                    cannot make animation.')
    

    def check_if_any_flows_arrived(self):
        if len(self.arrived_flows.keys()) == 0:
            sys.exit('Scheduling session ended, but no flows were recorded as \
                    having arrived. Consider whether the demand data you gave \
                    to the simulator actually contains non-zero sized messages \
                    which become flows.')
        else:
            pass


    def check_if_done(self):
        '''
        Checks if all flows (if flow centric) or all jobs (if job centric) have arrived &
        been completed
        '''
        if self.max_time is None:
            if self.demand.job_centric:
                if (len(self.arrived_jobs.keys()) != len(self.completed_jobs) and len(self.arrived_jobs.keys()) != 0) or len(self.arrived_jobs.keys()) != self.demand.num_demands:
                    return False
                else:
                    self.check_if_any_flows_arrived()
                    return True
            else:
                if (len(self.arrived_flows.keys()) != len(self.completed_flows) and len(self.arrived_flows.keys()) != 0) or len(self.arrived_flows.keys()) != self.demand.num_demands:
                    return False
                else:
                    self.check_if_any_flows_arrived()
                    return True

        else:
            if self.demand.job_centric:
                if self.curr_time >= self.max_time:
                    self.check_if_any_flows_arrived()
                    return True
                elif (len(self.arrived_jobs.keys()) != len(self.completed_jobs) and len(self.arrived_jobs.keys()) != 0) or len(self.arrived_jobs.keys()) != self.demand.num_demands:
                    return False
                else:
                    self.check_if_any_flows_arrived()
                    return True
            else:
                if self.curr_time >= self.max_time:
                    self.check_if_any_flows_arrived()
                    return True
                elif (len(self.arrived_flows.keys()) != len(self.completed_flows) and len(self.arrived_flows.keys()) != 0) or len(self.arrived_flows.keys()) != self.demand.num_demands:
                    return False
                else:
                    self.check_if_any_flows_arrived()
                    return True


    def get_path_edges(self, path):
        '''
        Takes a path and returns list of edges in the path

        Args:
        - path (list): path in which you want to find all edges

        Returns:
        - edges (list of lists): all edges contained within the path
        '''
        num_nodes = len(path)
        num_edges = num_nodes - 1
        edges = [path[edge:edge+2] for edge in range(num_edges)]

        return edges
    
    
    def check_flow_present(self, flow, flows):
        '''
        Checks if flow is present in a list of flows. Assumes the following 
        flow features are unique and unchanged properties of each flow:
        - flow size
        - source
        - destination
        - time arrived
        - flow_id
        - job_id

        Args:
        - flow (dict): flow dictionary
        - flows (list of dicts) list of flows in which to check if flow is
        present
        '''
        size = flow['size']
        src = flow['src']
        dst = flow['dst']
        time_arrived = flow['time_arrived']
        flow_id = flow['flow_id']
        job_id = flow['job_id']

        idx = 0
        for f in flows:
            if f['size']==size and f['src']==src and f['dst']==dst and f['time_arrived']==time_arrived and f['flow_id']==flow_id and f['job_id']==job_id:
                # flow found in flows
                return True, idx
            else:
                # flow not found, move to next f in flows
                idx += 1

        return False, idx
    
    def find_flow_idx(self, flow, flows):
        '''
        Finds flow idx in a list of flows. Assumes the following 
        flow features are unique and unchanged properties of each flow:
        - flow size
        - source
        - destination
        - time arrived
        - flow_id
        - job_id

        Args:
        - flow (dict): flow dictionary
        - flows (list of dicts) list of flows in which to find flow idx
        '''
        size = flow['size']
        src = flow['src']
        dst = flow['dst']
        time_arrived = flow['time_arrived']
        flow_id = flow['flow_id']
        job_id = flow['job_id']

        idx = 0
        for f in flows:
            if f['size']==size and f['src']==src and f['dst']==dst and f['time_arrived']==time_arrived and f['flow_id'] == flow_id and f['job_id'] == job_id:
                # flow found in flows
                return idx
            else:
                # flow not found, move to next f in flows
                idx += 1
        
        return sys.exit('Flow not found in list of flows')
    
    
    def check_if_channel_used(self, graph, edges, channel):
        '''
        Takes list of edges to see if any one of the edges have used a certain
        channel

        Args:
        - edges (list of lists): edges we want to check if have used certain
        channel
        - channel (label): channel we want to check if has been used by any
        of the edges

        Returns:
        - True/False
        '''
        channel_used = False
        
        num_edges = len(edges)
        for edge in range(num_edges):
            node_pair = edges[edge]
            capacity = graph[node_pair[0]][node_pair[1]]['channels'][channel]
            if round(capacity,0) != round(graph[node_pair[0]][node_pair[1]]['max_channel_capacity'],0):
                channel_used = True
                break
            else:
                continue

        return channel_used
        
    def set_up_connection(self, flow):
        '''
        Sets up connection between src-dst node pair by removing capacity from
        all edges in path connecting them. Also updates graph's global curr 
        network capacity used property
        
        Args:
        - flow (dict): flow dict containing flow info to set up
        '''
        path = flow['path']
        channel = flow['channel']
        flow_size = flow['size']

        edges = self.get_path_edges(path)

        num_edges = len(edges)
        for edge in range(num_edges):
            node_pair = edges[edge]
            # update edge property
            self.network[node_pair[0]][node_pair[1]]['channels'][channel] -= flow_size
            # update global graph property
            self.network.graph['curr_nw_capacity_used'] += flow_size
        self.network.graph['num_active_connections'] += 1
        
    

    def take_down_connection(self, flow):
        '''
        Removes established connection by adding capacity back onto all edges
        in the path connecting the src-dst node pair. Also updates graph's
        global curr network capacity used property

        Args:
        - flow (dict): flow dict containing info of flow to take down
        '''
        path = flow['path']
        channel = flow['channel']
        flow_size = flow['size']

        edges = self.get_path_edges(path)
        
        num_edges = len(edges)
        for edge in range(num_edges):
            node_pair = edges[edge]
            # update edge property
            self.network[node_pair[0]][node_pair[1]]['channels'][channel] += flow_size
            # update global graph property
            self.network.graph['curr_nw_capacity_used'] -= flow_size
        self.network.graph['num_active_connections'] -= 1
    
    def calc_flow_completion_times(self,times_arrived,times_completed):
        '''
        Calculates flow completion times of all recorded completed flows
        '''
        flow_completion_times = np.asarray(times_completed) - np.asarray(times_arrived)
        
        if len(flow_completion_times) == 0:
            average_fct, ninetyninth_percentile_fct = float('inf'), float('inf')
        else:
            average_fct = np.average(flow_completion_times)
            ninetyninth_percentile_fct = np.percentile(flow_completion_times,99)

        return flow_completion_times,average_fct,ninetyninth_percentile_fct

    
    def calc_job_completion_times(self,times_arrived,times_completed):
        job_completion_times = np.asarray(times_completed) - np.asarray(times_arrived)
        
        if len(job_completion_times) == 0:
            average_jct, ninetyninth_percentile_jct = float('inf'), float('inf')
        else:
            average_jct = np.average(job_completion_times)
            ninetyninth_percentile_jct = np.percentile(job_completion_times,99)
        

        return job_completion_times,average_jct,ninetyninth_percentile_jct
        
    
    def calc_total_info_transported(self):
        flow_sizes_transported = []
        num_completed_flows = len(self.completed_flows)

        for idx in range(num_completed_flows):
            flow_sizes_transported.append(self.completed_flows[idx]['size'])

        total_info_transported = sum(flow_sizes_transported)

        return flow_sizes_transported, total_info_transported

    def calc_total_info_arrived(self):
        flow_sizes_arrived = []
        num_arrived_flows = len(self.arrived_flows.keys())
        
        for idx in range(num_arrived_flows.keys()):
            flow_sizes_arrived.append(self.arrived_flow_dicts[idx]['size'])

        total_info_arrived = sum(flow_sizes_arrived)

        return flow_sizes_arrived, total_info_arrived
    
    def get_general_summary(self):
        _,self.info_arrived = self.calc_total_info_transported()
        _,self.info_transported = self.calc_total_info_transported()
        
        self.session_duration = self.time_last_flow_completed-self.time_first_flow_arrived
        
        self.load = self.info_arrived/(self.time_last_flow_arrived-self.time_first_flow_arrived)
        self.throughput = self.info_transported/self.session_duration
    
    
    def get_flow_summary(self):
        self.num_arrived_flows = len(self.arrived_flows.keys())
        self.num_completed_flows = len(self.completed_flows)
        self.num_dropped_flows = len(self.dropped_flows) + (self.num_arrived_flows-self.num_completed_flows)
        
        times_arrived = []
        times_completed = []
        for idx in range(self.num_completed_flows):
            times_arrived.append(self.completed_flows[idx]['time_arrived'])
            times_completed.append(self.completed_flows[idx]['time_completed'])
        _,self.avrg_fct,self.nn_fct = self.calc_flow_completion_times(times_arrived,times_completed)
        
        times_all_arrived = []
        for idx in range(self.num_arrived_flows):
            times_all_arrived.append(self.arrived_flow_dicts[idx]['time_arrived'])

        self.time_first_flow_arrived = min(times_all_arrived)
        self.time_last_flow_arrived = max(times_all_arrived)
        if len(times_completed) == 0:
            self.time_first_flow_completed = float('inf')
            self.time_last_flow_completed = float('inf')
        else:
            self.time_first_flow_completed = min(times_completed)
            self.time_last_flow_completed = max(times_completed)


    def get_job_summary(self):
        self.num_arrived_jobs = len(self.arrived_jobs.keys())
        self.num_completed_jobs = len(self.completed_jobs)
        self.num_dropped_jobs = len(self.dropped_jobs) + (self.num_arrived_jobs-self.num_completed_jobs)
        
        times_arrived = []
        times_completed = []
        for idx in range(self.num_completed_jobs):
            times_arrived.append(self.completed_jobs[idx]['time_arrived'])
            times_completed.append(self.completed_jobs[idx]['time_completed'])
        _,self.avrg_jct,self.nn_jct = self.calc_job_completion_times(times_arrived,times_completed)
        
        times_all_arrived = []
        for job in self.arrived_job_dicts:
            times_all_arrived.append(job['time_arrived'])

        self.time_first_job_arrived = min(times_all_arrived)
        self.time_last_job_arrived = max(times_all_arrived)
        if len(times_completed) == 0:
            self.time_first_job_completed = float('inf')
            self.time_last_job_completed = float('inf')
        else: 
            self.time_first_job_completed = min(times_completed)
            self.time_last_job_completed = max(times_completed)
        
        
        

    def get_scheduling_session_summary(self, print_summary=True):
        self.get_flow_summary()
        self.get_general_summary()
        if self.demand.job_centric:
            self.get_job_summary()
        if print_summary:
            self.print_scheduling_session_summary()
        



    def print_scheduling_session_summary(self):
        print('-=-=-=-=-=-=-= Scheduling Session Ended -=-=-=-=-=-=-=')
        print('SUMMARY:')
        print('~* General Info *~')
        print('Total session duration: {} time units'.format(self.session_duration))
        print('Total number of generated demands (jobs or flows): {}'.format(self.demand.num_demands))
        print('Total info arrived: {} info units'.format(self.info_arrived))
        print('Load: {} info unit demands arrived per unit time (from first to last flow arriving)'.format(self.load))
        print('Total info transported: {} info units'.format(self.info_transported))
        print('Throughput: {} info units transported per unit time'.format(self.throughput))
        
        print('\n~* Flow Info *~')
        if self.demand.job_centric:
            print('Total number generated data dependencies: {}'.format(self.demand.num_data_deps))
            print('Total number generated control dependencies: {}'.format(self.demand.num_control_deps))
        print('Total number generated flows (src!=dst,dependency_type==\'data_dep\'): {}'.format(self.demand.num_flows))
        print('Time first flow arrived: {} time units'.format(self.time_first_flow_arrived))
        print('Time last flow arrived: {} time units'.format(self.time_last_flow_arrived))
        print('Time first flow completed: {} time units'.format(self.time_first_flow_completed))
        print('Time last flow completed: {} time units'.format(self.time_last_flow_completed))
        print('Total number of demands that arrived and became flows: {}'.format(self.num_arrived_flows))
        print('Total number of flows that were completed: {}'.format(self.num_completed_flows))
        print('Total number of dropped flows + flows in queues at end of session: {}'.format(self.num_dropped_flows))
        print('Average FCT: {} time units'.format(self.avrg_fct))
        print('99th percentile FCT: {} time units'.format(self.nn_fct))
        
        
        if self.demand.job_centric:
            print('\n~* Job Info *~')
            print('Time first job arrived: {} time units'.format(self.time_first_job_arrived))
            print('Time last job arrived: {} time units'.format(self.time_last_job_arrived))
            print('Time first job completed: {} time units'.format(self.time_first_job_completed))
            print('Time last job completed: {} time units'.format(self.time_last_job_completed))
            print('Total number of job demands that arrived: {}'.format(self.num_arrived_jobs))
            print('Total number of job demands that were completed: {}'.format(self.num_completed_jobs))
            print('Total number of dropped jobs + jobs in queues at end of session: {}'.format(self.num_dropped_jobs))
            print('Total number of control dependencies that arrived: {}'.format(len(self.arrived_control_deps)))
            print('Total number of control deps that were data deps but had src==dst: {}'.format(len(self.arrived_control_deps_that_were_flows)))
            print('Average JCT: {} time units'.format(self.avrg_jct))
            print('99th percentile JCT: {} time units'.format(self.nn_jct))
   
        
    #def save_demand(self, path, name=None, overwrite=False):
    #    '''
    #    Save demand object of this simulation
    #    '''
    #    if name is None:
    #        name = 'demand_{}_jobcentric_{}'.format(str(self.demand.num_demands), str(self.demand.job_centric))
    #    else:
    #        # name already given
    #        pass
    #    
    #    filename = path+name+'.obj'
    #    if overwrite:
    #        # overwrite prev saved file
    #        pass
    #    else:
    #        # avoid overwriting
    #        v = 2
    #        while os.path.exists(str(filename)):
    #            filename = path+name+'_v{}'.format(v)+'.obj'
    #            v += 1
    #    filehandler = open(filename, 'wb')
    #    pickle.dump(dict(self.demand.__dict__), filehandler)
    #    filehandler.close()


    def save_sim(self, 
                 path, 
                 name=None, 
                 overwrite=False, 
                 zip_data=True, 
                 print_times=True):
        '''
        Save self (i.e. object) using pickle
        '''
        start = time.time()
        if name is None:
            name = 'sim_jobcentric_{}'.format(str(self.demand.num_demands), str(self.demand.job_centric))
        else:
            # name already given
            pass
        
        filename = path+name+'.obj'
        if overwrite:
            # overwrite prev saved file
            pass
        else:
            # avoid overwriting
            v = 2
            while os.path.exists(str(filename)):
                filename = path+name+'_v{}'.format(v)+'.obj'
                v += 1
        if zip_data:
            filehandler = bz2.open(filename, 'wb')
        else:
            filehandler = open(filename, 'wb')
        pickle.dump(dict(self.__dict__), filehandler)
        filehandler.close()
        end = time.time()
        if print_times:
            print('Time to save sim: {}'.format(end-start))


        

if __name__ == '__main__':
    import graphs  
    from demand import Demand
    from routing import RWA
    from scheduling import SRPT
    from scheduling import BASRPT
    import pickle
    import networkx as nx

    # config
    num_episodes = 1000
    num_k_paths = 1
    slot_size = 0.2
    max_time = 50
    path_figures = 'figures/'
    
    # either set simulator config or load prev saved demands
    #load_demands = None
    load_demands = 'pickles/demand/500_uniform_demands.obj' # path str or None
    if load_demands is None:
        num_channels = 2
        num_demands = 5
        min_flow_size = 1
        max_flow_size = 10
        ep_label = 'server'

        # initialise graph
        graph = graphs.gen_simple_graph(ep_label,num_channels)
        
        # initialise demand characteristics
        demand = Demand(graph, num_demands)
        demand.node_dist = demand.gen_uniform_node_dist()
        demand.flow_size_dist = demand.gen_uniform_val_dist(min_flow_size,max_flow_size,round_to_nearest=1) 
        demand.set_interarrival_dist(dist='exponential', params={'_beta': 0.1})
        demand.set_duration_dist(dist='exponential', params={'_beta': 1.0})
    else:
        filehandler = open(load_demands, 'rb')
        demand = pickle.load(filehandler)
        graph = demand.Graph
        edge_dict = nx.get_edge_attributes(graph, 'channels')
        num_channels = len(list(edge_dict[list(edge_dict.keys())[0]].keys()))

    # initialise routing agent
    rwa = RWA(graph['channel_names'], num_k_paths)
   
    # initialise scheduling agent
    #scheduler = SRPT(graph, rwa, slot_size) 
    scheduler = BASRPT(graph, rwa, slot_size, V=500) 

    # initialise dcn simulation environment
    env = DCN(demand, scheduler, max_time)

    # run simulations
    for episode in range(num_episodes):
        observation = env.reset(load_demands)
        while True:
            # print('Time: {}'.format(env.curr_time))
            # print('Observation:\n{}'.format(observation))
            action = scheduler.get_action(observation)
            # print('Action:\n{}'.format(action))
            observation, reward, done, info = env.step(action)
            # print('Observation:\n{}'.format(observation))
            if done:
                print('Episode finished.')
                env.get_scheduling_session_summary()
                env.print_scheduling_session_summary()
                env.plot_all_queue_evolution(path_figures+'scheduler/')
                break


        





