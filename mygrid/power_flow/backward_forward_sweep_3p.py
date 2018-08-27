from mygrid.util import Phasor, R, P
from mygrid.util import r2p, p2r
from mygrid.grid import Section,Auto_TransformerModel

import numpy as np

from pycallgraph import PyCallGraph
from pycallgraph.output import GraphvizOutput

from numba import jit

from functools import wraps


def calc_power_flow_profiling(dist_grid):
    graphviz = GraphvizOutput()
    graphviz.output_file = 'basic.png'

    with PyCallGraph(output=graphviz):
        calc_power_flow(dist_grid)

def calc_power_flow(dist_grid):

    # -------------------------
    # variables declarations
    # -------------------------
    
    max_iterations = 100
    converg_crt = 0.001
    converg = 1e6
    iter = 0

    #print('============================')
    #print('Distribution Grid {dg} Sweep'.format(dg=dist_grid.name))
    
    nodes_converg = dict()
    for node in dist_grid.load_nodes.values():
        nodes_converg[node.name] = 1e6

    # calculo da profundidade máxima da rede
    max_depth = np.max(dist_grid.load_nodes_tree.rnp.transpose()[:, 0].astype(int))

    nodes_depth_dict = _make_nodes_depth_dictionary(dist_grid)
    # -------------------------------------
    # main loop for power flow calculation
    # -------------------------------------
    while iter <= max_iterations and converg > converg_crt:
        iter += 1
        #print('<<<<-----------BFS------------>>>>')
        #print('Iteration: {iter}'.format(iter=iter))

        voltage_nodes = dict()
        for node in dist_grid.load_nodes.values():
            voltage_nodes[node.name] = node.vp
            node._calc_currents()

        # ----------------------------------
        # back-forward sweep implementation
        # ----------------------------------
        _dist_grid_sweep(dist_grid, max_depth, nodes_depth_dict)

        # -------------------------
        # convergence verification
        # -------------------------
        for node in dist_grid.load_nodes.values():
            nodes_converg[node.name] = np.mean(abs(voltage_nodes[node.name] - node.vp))
        converg = max(nodes_converg.values())
       # print('Max. diff between load nodes voltage values: {conv}'.format(conv=converg))

        # -------------------------
        # verificação de tensões 
        # das barras PV (se houverem).
        # -------------------------
    calc=False 
    for sections in dist_grid.sections.values():
        if isinstance(sections.transformer, Auto_TransformerModel) and not(sections.transformer_visited):
            calc=True
            sections.transformer_visited=True

            if sections.transformer.compesator_active:
                va,vb,vc=sections.transformer.controler_voltage(sections.n2.ip[0],sections.n2.ip[1],sections.n2.ip[2],\
                    sections.n2.vp[0],sections.n2.vp[1],sections.n2.vp[2])

            else:
                va=sections.n2.vp[0]
                vb=sections.n2.vp[1]
                vc=sections.n2.vp[2]

    
            sections.transformer.define_parameters(va,vb,vc)
            sections._set_transformer_model()
            
    if calc:
        for node in dist_grid.load_nodes.values():
            node.config_voltage(voltage=node.voltage)

        calc_power_flow(dist_grid)

    i=10
    while i >0:

        DG_unconv_ = _nodes_out_limit(dist_grid)
        if DG_unconv_ != []:

            for sections in dist_grid.sections.values():

                if isinstance(sections.transformer, Auto_TransformerModel):
                    sections.transformer_visited=False

            _define_power_insertion(DG_unconv_, dist_grid)

            for node in dist_grid.load_nodes.values():
                node.config_voltage(voltage=node.voltage)
                node._calc_currents()
            
            power_flow(dist_grid)

        else:
            

            for node in dist_grid.load_nodes.values():
                if (node.generation != None and node.generation.type == 'PV') and \
                    node.generation.limit_PV :

                    print("{0} exceeded the limit Generation ".format(node.generation.name))
            return 

        i-=1

    print("Load flow did not converge")
    return

def _dist_grid_sweep(dist_grid, max_depth, nodes_depth_dict):
    """ Função que varre a dist_grid pelo
    método varredura direta/inversa"""

    dist_grid_rnp = dist_grid.load_nodes_tree.rnp
    load_nodes_tree = dist_grid.load_nodes_tree.tree

    depth = max_depth

    # ----------------------------------------------
    # Inicio da varredura inversa
    # ----------------------------------------------
    #print('Backward Sweep phase <<<<----------')
    # seção do cálculo das potências partindo dos
    # nós com maiores profundidades até o nó raíz
    while depth >= 0:
        # guarda os nós com maiores profundidades.
        nodes = nodes_depth_dict[str(depth)]

        # decrementodo da profundidade.
        depth -= 1

        # for que percorre os nós com a profundidade
        # armazenada na variável depth
        for node in nodes:

            # atualiza o valor de corrente passante com o valor
            # da corente da carga para que na prox. iteração 
            # do fluxo de carga não ocorra acúmulo.
            node.ip = node.i

            downstream_neighbors = _get_downstream_neighbors_nodes(node, dist_grid)

            # verifica se não há neighbor a jusante,
            # se não houverem o nó de carga analisado
            # é o último do ramo.
            if downstream_neighbors == []:
                continue
            else:
                # precorre os nos a jusante do no atual
                # para calculo de fluxos passantes
                for downstream_node in downstream_neighbors:
                    # chama a função busca_trecho para definir
                    # quais sections estão entre o nó atual e o nó a jusante
                    section = _search_section(node, downstream_node, dist_grid)
                    
                    # ------------------------------------
                    # Equacionamento: Calculo de corretes
                    # ------------------------------------
                    node.ip += np.dot(section.c, downstream_node.vp) + \
                               np.dot(section.d, downstream_node.ip)
                    # -------------------------------------
                    #print(node.name + '<<<---' + downstream_node.name)

            
    #print('Forward Sweep phase ---------->>>>')

    depth = 1
    # seção do cálculo de atualização das tensões
    while depth <= max_depth:
        # salva os nós de carga a montante
        nodes = nodes_depth_dict[str(depth)]

        # percorre os nós para guardar a árvore do nó requerido
        for node in nodes:
            upstream_node = _get_upstream_neighbor_node(node, dist_grid)
            section = _search_section(node, upstream_node, dist_grid)

            # ------------------------------------
            # Equacionamento: Calculo de tensoes
            # ------------------------------------
            node.vp = np.dot(section.A, upstream_node.vp) - \
                      np.dot(section.B, node.ip)
            # ------------------------------------
            #print(upstream_node.name + '--->>>' + node.name)
        depth += 1


def _make_nodes_depth_dictionary(dist_grid):
    
    dist_grid_rnp = dist_grid.load_nodes_tree.rnp

    nodes_depth_dict = dict()
    for node_depth in dist_grid_rnp.transpose():
        depth = node_depth[0]
        node = node_depth[1]
        if depth in nodes_depth_dict.keys():
            nodes_depth_dict[depth] += [dist_grid.load_nodes[node]]
        else:
            nodes_depth_dict[depth] = [dist_grid.load_nodes[node]]
    return nodes_depth_dict


def _get_downstream_neighbors_nodes_cached(f):
    cache = dict()
    @wraps(f)
    def inner_get_downstream_neighbors_nodes(arg1, arg2):
        if arg1 not in cache:
            cache[arg1] = f(arg1, arg2)
        return cache[arg1]
    return inner_get_downstream_neighbors_nodes


@_get_downstream_neighbors_nodes_cached
def _get_downstream_neighbors_nodes(node, dist_grid):

    load_nodes_tree = dist_grid.load_nodes_tree.tree
    dist_grid_rnp_dict = dist_grid.load_nodes_tree.rnp_dict()

    neighbors = load_nodes_tree[node.name]
    downstream_neighbors = list()

    # for que percorre a árvore de cada nó de carga vizinho
    for neighbor in neighbors:        
        # verifica se a profundidade do neighbor é maior
        if int(dist_grid_rnp_dict[neighbor]) > int(dist_grid_rnp_dict[node.name]):
            downstream_neighbors.append(dist_grid.load_nodes[neighbor])

    return downstream_neighbors


def _get_upstream_neighbor_node_cached(f):
    cache = dict()
    @wraps(f)
    def inner_get_upstream_neighbor_node(arg1, arg2):
        if arg1 not in cache:
            cache[arg1] = f(arg1, arg2)
        return cache[arg1]
    return inner_get_upstream_neighbor_node

@_get_upstream_neighbor_node_cached
def _get_upstream_neighbor_node(node, dist_grid):
    
    load_nodes_tree = dist_grid.load_nodes_tree.tree
    dist_grid_rnp_dict = dist_grid.load_nodes_tree.rnp_dict()

    neighbors = load_nodes_tree[node.name]
    
    upstream_neighbors = list()

    # verifica quem é neighbor do nó desejado.
    for neighbor in neighbors:
        if int(dist_grid_rnp_dict[neighbor]) < int(dist_grid_rnp_dict[node.name]):
            upstream_neighbors.append(dist_grid.load_nodes[neighbor])

    # retorna o primeiro neighbor a montante
    return upstream_neighbors[0]


def _search_section_cached(f):
    cache = dict()
    @wraps(f)
    def inner_search_section(arg1, arg2, arg3):
        if (arg1, arg2) not in cache or (arg2, arg1) not in cache:
            cache[(arg1, arg2)] = f(arg1, arg2, arg3)
        return cache[(arg1, arg2)]
    return inner_search_section


@_search_section_cached
def _search_section(n1, n2, dist_grid):
    """Função que busca sections em um alimendador entre os nos
      n1 e n2"""

    if (n1, n2) in dist_grid.sections_by_nodes.keys():
        return dist_grid.sections_by_nodes[(n1, n2)]
    elif (n2, n1) in dist_grid.sections_by_nodes.keys():
        return dist_grid.sections_by_nodes[(n2, n1)]


def _nodes_out_limit(dist_grid):
    
    root_3=np.sqrt(3)
    DG_unconv_ = list()
    for node in dist_grid.load_nodes.values():



        if (node.generation != None and node.generation.type == 'PV'):

            vaa = np.abs(node.vp[0]) / np.abs(node.voltage/root_3)
            vbb = np.abs(node.vp[1]) / np.abs(node.voltage/root_3)
            vcc = np.abs(node.vp[2]) / np.abs(node.voltage/root_3)
            vphase_max = max([vaa,vbb,vcc])
            vphase_min = min([vaa,vbb,vcc])
               
            if node.generation.defective_phase != None:

                v_defective=np.abs(node.vp[node.generation.defective_phase]/np.abs(node.voltage/root_3))
                if np.abs(v_defective-node.generation.Vspecified)>node.generation.DV_presc:
                    
                    DG_unconv_.append(node)

            else:
                if (vphase_min) < node.generation.Vmin:

                    if vaa==vphase_min:
                        node.generation.defective_phase=0
                    if vbb==vphase_min:
                        node.generation.defective_phase=1
                    if vcc==vphase_min:
                        node.generation.defective_phase=2

                    
                    DG_unconv_.append(node)
                    

                elif (vphase_max) > node.generation.Vmax:
                    if vaa==vphase_max:
                        node.generation.defective_phase=0
                    if vbb==vphase_max:
                        node.generation.defective_phase=1
                    if vcc==vphase_max:
                        node.generation.defective_phase=2

                    
                    DG_unconv_.append(node)  

    return DG_unconv_


def _define_power_insertion(DG_unconv_, dist_grid):

    Vnom=13.8e3/np.sqrt(3)
    z_base = (Vnom)**2 / 100e6
    I_base = 100e6 / Vnom

    reactan_mat_ = np.ones((len(DG_unconv_), len(DG_unconv_))) * (0.0 + 1.0j)
    equal_section_ = {}
    name_root = list(dist_grid.sectors[dist_grid.root].load_nodes.values())[0].name
    section_list = list()
    for i in range(len(DG_unconv_)):

       
        dg_source=sections_path_to_root(dist_grid, DG_unconv_[i].name)
        section_list.append(dg_source)
        reactan_mat_[i, i] = sum_imped(dg_source)



    for i in range(len(section_list)):
        for j in range(i+1,len(section_list)):
            comm_path=list()
            for x in section_list[i]:
                for y in section_list[j]:
                    if x==y:
                        comm_path.append(x)
            reactan_mat_[i, j] =reactan_mat_[j, i]= sum_imped(comm_path)


    inv_X = np.linalg.inv(reactan_mat_)
    delta_I = {}

 

    delta_V = np.ones((len(DG_unconv_),1))

    for i in range(len(DG_unconv_)):
        v=np.abs(DG_unconv_[i].vp0[0])
        Vcurrent =np.abs(DG_unconv_[i].vp[DG_unconv_[i].generation.defective_phase][0]/v)

        ves=np.abs(DG_unconv_[i].generation.Vspecified)

        delta_V[i,0] =ves - (Vcurrent)

    
        
    delta_I = inv_X.dot(delta_V)
    


    for i in range(len(DG_unconv_)):
        vmin_dg = np.min([np.abs(x) for x in DG_unconv_[i].vp])
        vln=np.abs(DG_unconv_[i].voltage)/np.sqrt(3)
        Q=delta_I[i][0]*(100e6)
        
        DG_unconv_[i].generation.update_Q(Q,Q,Q)
        print(DG_unconv_[i].generation.P/3)






def sections_path_to_root(dist_grid, n2):

    
    root=list(dist_grid.sectors[dist_grid.root].load_nodes.values())[0].name
    if n2 == root:
        return list()

    for section in dist_grid.sections.values():
        name=section.name

        if not(dist_grid.sections[name].switch !=None and dist_grid.sections[name].switch.state==0):

            if dist_grid.sections[name].n1.name == n2:
                

                section_list=sections_path_to_root(dist_grid,dist_grid.sections[name].n2.name)
                section_list.append(dist_grid.sections[name])

                return section_list


            elif dist_grid.sections[name].n2.name == n2:

                section_list=sections_path_to_root(dist_grid,dist_grid.sections[name].n1.name)
                section_list.append(dist_grid.sections[name])

                return section_list

def sum_imped(sections):

    z=0
    for i in sections:

        if i.line_model is not None:
            z_base = np.abs((i.n1.vp0[0])**2 / 100e6)
            z +=np.imag(i.Z012[1,1])*1j/z_base

        else:

            z_base = np.abs((i.n2.vp0[0])**2 / 100e6)
            z +=np.imag(i.transformer.zt_012[1,1])*1j/z_base

    return z
