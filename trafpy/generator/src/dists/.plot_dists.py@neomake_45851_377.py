'''Module for plotting node and value distributions.'''

from trafpy.generator.src import tools
from trafpy.generator.src.dists import val_dists 
from trafpy.generator.src.dists import node_dists 

import numpy as np
import copy
np.set_printoptions(threshold=np.inf)
import matplotlib.pyplot as plt
import matplotlib
from matplotlib import colors
import seaborn as sns
from statsmodels.distributions.empirical_distribution import ECDF
from scipy import stats
import networkx as nx
from nxviz.plots import CircosPlot
import json

# import warnings # for catching warnings rather than just exceptions
# warnings.filterwarnings('error')



def plot_node_dist(node_dist, 
                   eps=None,
                   node_to_index_dict=None,
                   add_labels=False, 
                   add_ticks=False,
                   cbar_label='Fraction',
                   chord_edge_width_range=[1, 25],
                   chord_edge_display_threshold=0.3,
                   show_fig=False):
    '''Plots network node demand distribution as (1) 2d matrix and (2) chord diagram.

    Args:
        node_dist (list or 2d numpy array): Source-destination pair probabilities 
            of being chosen. Must be either a 2d numpy matrix of probabilities or 
            a 1d list/array of node pair probabilities.
        eps (list): List of node endpoint labels.
        node_to_index_dict (dict): Maps node labels (keys) to integer indices (values).
        add_labels (bool): Whether or not to node labels to plot.
        add_ticks (bool): Whether or not to add ticks to x- and y-axis.
        cbar_label (str): Label for colour bar.
        chord_edge_width_range (list): Range [min, max] of edge widths for chord diagram.
        chord_edge_display_threshold (float): Float between 0 and 1 of fraction/probability
            of end point pair above which to draw an edge on the chord diagram. E.g. if 
            0.3, will draw all edges which have a load pair higher than 0.3*max_pair_load.
            Higher threshold -> fewer edges plotted -> can increase clarity.
        show_fig (bool): Whether or not to plot and show fig. If True, will
            return and display fig.

    Returns:
        matplotlib.figure.Figure: node distribution plotted as a 2d matrix. 

    '''
    # print(node_dist)
    if type(node_dist[0]) == str and eps is None:
        eps = list(set(node_dist))
    # else:
        # assert eps is not None, 'must provide list of end points as arg if node_dist contains no endpoint labels.'
    else:
        eps = [str(i) for i in range(np.array(node_dist).shape[0])]

    if node_to_index_dict is None:
        _,_,node_to_index_dict,_=tools.get_network_params(eps) 

    figs = []

    # 1. PLOT NODE DIST
    fig = plt.matshow(node_dist, cmap='YlOrBr')
    plt.style.use('default')
    cbar = plt.colorbar()
    if add_labels == True:
        for (i, j), z in np.ndenumerate(node_dist):
            plt.text(j, 
                     i, 
                     '{}'.format(z), 
                     ha='center', 
                     va='center',
                     bbox = dict(boxstyle='round', 
                     facecolor='white', 
                     edgecolor='0.3'))
    plt.xlabel('Destination (Node #)')
    plt.ylabel('Source (Node #)')
    cbar.ax.set_ylabel(cbar_label, rotation=270, x=0.5)
    if add_ticks:
        plt.xticks([node_to_index_dict[node] for node in eps])
        plt.yticks([node_to_index_dict[node] for node in eps])
    figs.append(fig)

    if show_fig:
        plt.show()


    # 2. PLOT CHORD DIAGRAM
    probs = node_dists.assign_matrix_to_probs(eps, node_dist)
    graph = nx.Graph()
    node_to_load = {}
    _, _, node_to_index, index_to_node = tools.get_network_params(eps)
    max_pair_load = max(list(probs.values()))
    for pair in probs.keys():
        pair_load = probs[pair]
        p = json.loads(pair)
        src, dst = str(p[0]), str(p[1])
        if src not in node_to_load:
            node_to_load[src] = pair_load
        else:
            node_to_load[src] += pair_load
        if dst not in node_to_load:
            node_to_load[dst] = pair_load
        else:
            node_to_load[dst] += pair_load
        if pair_load > chord_edge_display_threshold*max_pair_load:
            graph.add_weighted_edges_from([(src, dst, pair_load)])
        else:
            graph.add_nodes_from([src, dst])

    nodelist = [n for n in graph.nodes]
    try:
        ws = _rescale([float(graph[u][v]['weight']) for u, v in graph.edges], newmin=chord_edge_width_range[0], newmax=chord_edge_width_range[1])
    except ZeroDivisionError:
        raise Exception('Src-dst edge weights in chord diagram are all the same, leading to 0 rescaled values. Decrease chord_edge_display_threshold to ensure a range of edge values are included in the chord diagram.')
    edgelist = [(str(u),str(v),{"weight":ws.pop(0)}) for u,v in graph.edges]

    graph2 = nx.Graph()
    graph2.add_nodes_from(nodelist)
    graph2.add_edges_from(edgelist)

    for v in graph2:
        graph2.nodes[v]['load'] = node_to_load[v]

    fig2 = plt.figure()
    plt.set_cmap('YlOrBr')
    plt.style.use('default')
    chord_diagram = CircosPlot(graph2, 
                               node_labels=True,
                               edge_width='weight',
                               # edge_color='weight',
                               # node_size='load',
                               node_grouping='load',
                               node_color='load')
    chord_diagram.draw()
    figs.append(fig2)
    
    if show_fig:
        plt.show()




    return figs


def _rescale(l, newmin, newmax):
    '''Rescales list l values to range between newmin and newmax.'''
    arr = list(l)
    return [round((x-min(arr))/(max(arr)-min(arr))*(newmax-newmin)+newmin,2) for x in arr]

def plot_dict_scatter(_dict,
                      logscale=False,
                      rand_var_name='Random Variable',
                      ylabel='Probability',
                      xlim=None,
                      marker_size=30,
                      font_size=20,
                      show_fig=False):
    '''
    Plots scatter of dict with keys (x-axis random variables) values (corresponding
    y-axis values) pairs.

    This is useful for plotting discrete probability distributions i.e. _dict keys are
    random variable values, _dict values are their respective probabilities of
    occurring.
    '''
    fig = plt.figure()
    plt.style.use('ggplot')
    if logscale:
        ax = plt.gca()
        ax.set_xscale('log')

    plt.scatter(list(_dict.keys()), list(_dict.values()), s=marker_size)
    plt.xlabel(rand_var_name, fontsize=font_size)
    plt.ylabel(ylabel, fontsize=font_size)

    if xlim is not None:
        plt.xlim(xlim)

    if show_fig:
        plt.show()

    return fig





def plot_val_dist(rand_vars, 
                  dist_fit_line=None, 
                  xlim=None, 
                  logscale=False,
                  transparent=False,
                  rand_var_name='Random Variable', 
                  prob_rand_var_less_than=None,
                  num_bins=0,
                  plot_cdf=True,
                  plot_horizontally=True,
                  fig_scale=1,
                  font_size=20,
                  show_fig=False):
    '''Plots (1) probability distribution and (2) cumulative distribution function.
    
    Args:
        rand_vars (list): Random variable values.
        dist_fit_line (str): Line to fit to named distribution. E.g. 'exponential'.
            If not plotting a named distribution, leave as None.
        xlim (list): X-axis limits of plot. E.g. xlim=[0,10] to plot random
            variable values between 0 and 10.
        logscale (bool): Whether or not plot should have logscale x-axis and bins.
        transparent (bool): Whether or not to make plot bins slightly transparent.
        rand_var_name (str): Name of random variable to label plot's x-axis.
        num_bins (int): Number of bins to use in plot. Default is 0, in which
            case the number of bins chosen will be automatically selected.
        plot_cdf (bool): Whether or not to plot the CDF as well as the probability
            distribution.
        plot_horizontally (bool): Wheter to plot PDF and CDF horizontally (True)
            or vertically (False).
        fig_scale (int/float): Scale by which to multiply figure size.
        font_size (int): Size of axes ticks and titles.
        show_fig (bool): Whether or not to plot and show fig. If True, will
            return and display fig.
    
    Returns:
        matplotlib.figure.Figure: node distribution plotted as a 2d matrix. 

    '''

    if num_bins==0:
        histo, bins = np.histogram(rand_vars,density=True,bins='auto')
    else:
        histo, bins = np.histogram(rand_vars,density=True,bins=num_bins)
    if transparent:
        alpha=0.30
    else:
        alpha=1.0
    # HISTOGRAM
    if plot_horizontally:
        fig = plt.figure(figsize=(15*fig_scale,5*fig_scale))
        plt.subplot(1,2,1)
    else:
        fig = plt.figure(figsize=(10*fig_scale,15*fig_scale))
        plt.subplot(2,1,1)
    plt.style.use('ggplot')
    if logscale:
        ax = plt.gca()
        ax.set_xscale('log')
        if bins[0] == 0:
            bins[0] = 1
        logbins = np.logspace(np.log10(bins[0]),np.log10(bins[-1]),len(bins))
        plotbins = logbins
    else:
        plotbins = bins
    


    plt.hist(rand_vars,
             bins=plotbins,
             align='mid',
             color='tab:red',
             edgecolor='tab:red',
             alpha=alpha)
    
    if dist_fit_line is None:
        pass
    elif dist_fit_line == 'exponential':
        loc, scale = stats.expon.fit(rand_vars, floc=0)
        y = stats.expon.pdf(plotbins, loc, scale)
    elif dist_fit_line == 'lognormal':
        shape, loc, scale = stats.lognorm.fit(rand_vars, floc=0)
        y = stats.lognorm.pdf(plotbins, shape, loc, scale)
    elif dist_fit_line == 'weibull':
        shape, loc, scale = stats.weibull_min.fit(rand_vars, floc=0)
        y = stats.weibull_min.pdf(plotbins, shape, loc, scale)
    elif dist_fit_line == 'pareto':
        shape, loc, scale = stats.pareto.fit(rand_vars, floc=0)
        y = stats.pareto.pdf(plotbins, shape, loc, scale)

    plt.xlabel(rand_var_name, fontsize=font_size)
    plt.ylabel('Counts', fontsize=font_size)
    try:
        plt.xlim(xlim)
    except NameError:
        pass
    
    if plot_cdf:
        # CDF
        # empirical hist
        if plot_horizontally:
            plt.subplot(1,2,2)
        else:
            plt.subplot(2,1,2)
        if logscale:
            ax = plt.gca()
            ax.set_xscale('log')
        else:
            pass
        n,bins_temp,patches = plt.hist(rand_vars, 
                                       bins=plotbins, 
                                       cumulative=True,
                                       density=True,
                                       histtype='step',
                                       color='tab:red',
                                       edgecolor='tab:red')
        patches[0].set_xy(patches[0].get_xy()[:-1])
        # theoretical line
        ecdf = ECDF(rand_vars)
        plt.plot(ecdf.x, ecdf.y, alpha=0.5, color='tab:blue')
        plt.xlabel(rand_var_name, fontsize=font_size)
        plt.ylabel('CDF', fontsize=font_size)
        try:
            plt.xlim(xlim)
        except NameError:
            pass
        plt.ylim(top=1)
    
    # PRINT ANY EXTRA ANALYSIS OF DISTRIBUTION
    if prob_rand_var_less_than is None:
        pass
    else:
        for prob in prob_rand_var_less_than:
            print('P(x<{}): {}'.format(prob, ecdf(prob)))
    
    if show_fig:
        plt.show()

    return fig


def plot_val_bar(x_values,
                 y_values,
                 ylabel='Random Variable',
                 ylim=None,
                 xlabel=None,
                 plot_x_ticks=True,
                 bar_width=0.35,
                 show_fig=False):
    '''Plots standard bar chart.'''
    x_pos = [x for x in range(len(x_values))]

    fig = plt.figure()
    plt.style.use('ggplot')

    plt.bar(x_pos, y_values, bar_width)

    plt.ylabel(ylabel)
    if plot_x_ticks:
        plt.xticks(x_pos, (x_val for x_val in x_values))
    if xlabel is not None:
        plt.xlabel(xlabel)

    try:
        plt.ylim(ylim)
    except NameError:
        pass

    if show_fig:
        plt.show()

    return fig


def plot_val_line(plot_dict={},
                     xlabel='Random Variable',
                     ylabel='Random Variable Value',
                     ylim=None,
                     linewidth=1,
                     alpha=1,
                     vertical_lines=[],
                     show_fig=False):
    '''Plots line plot.

    plot_dict= {'class_1': {'x_values': [0.1, 0.2, 0.3], 'y_values': [20, 40, 80]},
                'class_2': {'x_values': [0.1, 0.2, 0.3], 'y_values': [80, 60, 20]}}

    '''
    keys = list(plot_dict.keys())

    fig = plt.figure()
    plt.style.use('ggplot')

    class_colours = iter(sns.color_palette(palette='hls', n_colors=len(keys), desat=None))
    for _class in sorted(plot_dict.keys()):
        plt.plot(plot_dict[_class]['x_values'], plot_dict[_class]['y_values'], c=next(class_colours), linewidth=linewidth, alpha=alpha, label=str(_class))
        for vline in vertical_lines:
            plt.axvline(x=vline, color='r', linestyle='--')

    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    if ylim is not None:
        plt.ylim(bottom=ylim[0], top=ylim[1])
    plt.legend()

    if show_fig:
        plt.show()
    

    return fig

def plot_val_scatter(plot_dict={},
                     xlabel='Random Variable',
                     ylabel='Random Variable Value',
                     alpha=1,
                     marker_size=30,
                     marker_style='.',
                     logscale=False,
                     show_fig=False):
    '''Plots scatter plot.

    plot_dict= {'class_1': {'x_values': [0.1, 0.2, 0.3], 'y_values': [20, 40, 80]},
                'class_2': {'x_values': [0.1, 0.2, 0.3], 'y_values': [80, 60, 20]}}

    '''
    keys = list(plot_dict.keys())
    # num_vals = len(plot_dict[keys[0]]['x_values'])
    # for key in keys:
        # if len(plot_dict[key]['x_values']) != num_vals or len(plot_dict[key]['y_values']) != num_vals:
            # raise Exception('Must have equal number of x and y values to plot.')

    fig = plt.figure()
    plt.style.use('ggplot')

    class_colours = iter(sns.color_palette(palette='hls', n_colors=len(keys), desat=None))
    for _class in sorted(plot_dict.keys()):
        plt.scatter(plot_dict[_class]['x_values'], plot_dict[_class]['y_values'], c=next(class_colours), s=marker_size, alpha=alpha, marker=marker_style, label=str(_class))

    if logscale:
        ax = plt.gca()
        ax.set_xscale('log')

    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.legend()

    if show_fig:
        plt.show()
    

    return fig

def plot_multiple_kdes(plot_dict={},
                       plot_hist=False,
                       xlabel='Random Variable',
                       ylabel='Density',
                       logscale=False,
                       show_fig=False):
    fig = plt.figure()
    plt.style.use('ggplot')
    if logscale:
        ax = plt.gca()
        ax.set_xscale('log')

    keys = list(plot_dict.keys())
    class_colours = iter(sns.color_palette(palette='hls', n_colors=len(keys), desat=None))
    for _class in sorted(plot_dict.keys()):
        color = next(class_colours)
        sns.distplot(plot_dict[_class]['rand_vars'], hist=plot_hist, kde=True, norm_hist=True, label=str(_class))

    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.legend()

    if show_fig:
        plt.show()

    return fig


def plot_val_cdf(plot_dict={},
                 xlabel='Random Variable',
                 ylabel='CDF',
                 logscale=False,
                 plot_points=True,
                 complementary_cdf=False,
                 show_fig=False):
    '''Plots CDF plot.

    plot_dict= {'class_1': {'rand_vars': [0.1, 0.1, 0.3],
                'class_2': {'rand_vars': [0.2, 0.2, 0.3]}}

    '''
    fig = plt.figure()
    plt.style.use('ggplot')

    if logscale:
        ax = plt.gca()
        ax.set_xscale('log')

    keys = list(plot_dict.keys())
    class_colours = iter(sns.color_palette(palette='hls', n_colors=len(keys), desat=None))
    for _class in sorted(plot_dict.keys()):
        ecdf = ECDF(plot_dict[_class]['rand_vars'])
        colour = next(class_colours)
        if complementary_cdf:
            ylabel = 'Complementary CDF'
            plt.plot(ecdf.x, 1-ecdf.y, c=colour, label=str(_class))
            if plot_points: 
                plt.scatter(ecdf.x, 1-ecdf.y, c=colour)
        else:
            plt.plot(ecdf.x, ecdf.y, c=colour, label=str(_class))
            if plot_points:
                plt.scatter(ecdf.x, ecdf.y, c=colour)
    plt.ylim(top=1, bottom=0)

    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.legend()

    if show_fig:
        plt.show()

    return fig


def plot_val_stacked_bar(plot_dict={},
                         ylabel='Random Variable',
                         ylim=None,
                         bar_width=0.35,
                         show_fig=False):
    '''Plots stacked bar chart.

    E.g. plot_dict given should be of the form:

    plot_dict= {'class_1': {'x_values': ['Uni DCN', 'Private DCN', 'Cloud DCN'], 'y_values': [20, 40, 80]},
    'class_2': {'x_values': ['Uni DCN', 'Private DCN', 'Cloud DCN'], 'y_values': [80, 60, 20]}}

    ylim=[0,100]

    '''


    keys = list(plot_dict.keys())
    num_vals = len(plot_dict[keys[0]]['x_values'])
    for key in keys:
        if len(plot_dict[key]['x_values']) != num_vals or len(plot_dict[key]['y_values']) != num_vals:
            raise Exception('Must have equal number of x and y values to plot if want to stack bars.')
    x_pos = [x for x in range(num_vals)]


    fig = plt.figure()
    plt.style.use('ggplot')

    plots = {}
    curr_bottom = None # init bottom y coords of bar to plot
    for _class in sorted(plot_dict.keys()):
        plots[_class] = plt.bar(x_pos, plot_dict[_class]['y_values'], bar_width, bottom=curr_bottom)
        # update bottom y coords for next bar
        curr_bottom = plot_dict[_class]['y_values']

    plt.ylabel(ylabel)
    plt.xticks(x_pos, (x_val for x_val in plot_dict[_class]['x_values']))
    plt.legend((plots[key][0] for key in list(plots.keys())), (_class for _class in (plot_dict.keys())))

    try:
        plt.ylim(ylim)
    except NameError:
        pass

    if show_fig:
        plt.show()

    return fig



def plot_demand_slot_colour_grid(grid_demands, title=None, xlim=None, show_fig=False):
    # set colours
    # class_colours = sns.color_palette(palette='hls', n_colors=None, desat=None)
    # cmap = colors.ListedColormap(class_colours
    cmap = None

    # plot grid
    fig, ax = plt.subplots()
    c = ax.pcolor(grid_demands, cmap=cmap)
    plt.xlabel('Time Slot')
    plt.ylabel('Flow Slot')

    if title is not None:
        ax.set_title(title)

    if xlim is not None:
        plt.xlim(xlim)

    if show_fig:
        plt.show()

    return fig














