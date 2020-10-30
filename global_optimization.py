import random
from copy import deepcopy
from math import sqrt
from time import time
from typing import List, Dict, Any, Tuple
from matplotlib import cm
from matplotlib.colors import Normalize
import numpy as np
from PIL import Image
from scipy.ndimage import distance_transform_edt

import cell
import optimization

useDistanceObjective = False


def check_constraints(config, imageshape, cells: List[cell.Bacilli], pairs: List[Tuple[cell.Bacilli, cell.Bacilli]] = None):
    max_displacement = config['bacilli.maxSpeed'] / config['global.framesPerSecond']
    max_rotation = config['bacilli.maxSpin'] / config['global.framesPerSecond']
    min_growth = config['bacilli.minGrowth']
    max_growth = config['bacilli.maxGrowth']
    min_width = config['bacilli.minWidth']
    max_width = config['bacilli.maxWidth']
    min_length = config['bacilli.minLength']
    max_length = config['bacilli.maxLength']

    for cell in cells:
        if not (0 <= cell.x < imageshape[1] and 0 <= cell.y < imageshape[0]):
            return False
        elif cell.width < min_width or cell.width > max_width:
            return False
        elif not (min_length < cell.length < max_length):
            return False
        elif config["simulation"]["image.type"] == "graySynthetic" and cell.opacity < 0:
            return False

    for cell1, cell2 in pairs:
        displacement = sqrt(np.sum((cell1.position - cell2.position)) ** 2)
        if displacement > max_displacement:
            return False
        elif abs(cell2.rotation - cell1.rotation) > max_rotation:
            return False
        elif not (min_growth < cell2.length - cell1.length < max_growth):
            return False

    return True


class CellNodeM:
    def __init__(self, cell: cell.Bacilli, parent: 'CellNodeM' = None):
        self.cell = cell
        self.parent = parent
        self.children: List[CellNodeM] = []

    def __repr__(self):
        return f'<name={self.cell.name}, parent={self.parent.cell.name if self.parent else None}, children={[node.cell.name for node in self.children]}>'

    @property
    def grandchildren(self):
        grandchildren = []
        for child in self.children:
            grandchildren.extend(child.children)
        return grandchildren

    def make_child(self, cell: cell.Bacilli):
        child = CellNodeM(cell, self)
        self.children.append(child)
        return child


class FrameM:
    def __init__(self, simulation_config, prev: 'FrameM' = None):
        #simulation_config is passed by value and modified within the object
        self.node_map: Dict[str, CellNodeM] = {}
        self.prev = prev
        self.simulation_config = simulation_config.copy()
     
    def __repr__(self):
        return str(list(self.node_map.values()))

    @property
    def nodes(self) -> List[CellNodeM]:
        return list(self.node_map.values())

    def add_cell(self, cell: cell.Bacilli):
        if cell.name in self.node_map:
            self.node_map[cell.name].cell = cell
        elif self.prev and cell.name in self.prev.node_map:
            self.node_map[cell.name] = self.prev.node_map[cell.name].make_child(cell)
        elif self.prev and cell.name[:-1] in self.prev.node_map:
            self.node_map[cell.name] = self.prev.node_map[cell.name[:-1]].make_child(cell)
        else:
            self.node_map[cell.name] = CellNodeM(cell)


class LineageM:
    def __init__(self, simulation_config):
        self.frames = [FrameM(simulation_config)]

    def __repr__(self):
        return '\n'.join([str(frame) for frame in self.frames])

    @property
    def total_cell_count(self):
        return sum(len(frame.node_map) for frame in self.frames)

    def count_cells_in(self, start, end):
        if start is None or start < 0:
            start = 0
        if end is None or end > len(self.frames):
            end = len(self.frames)
        return sum(len(frame.node_map) for frame in self.frames[start:end])

    def forward(self):
        self.frames.append(FrameM(self.frames[-1].simulation_config, self.frames[-1]))

    def copy_forward(self):
        self.forward()
        for cell_node in self.frames[-2].nodes:
            self.frames[-1].add_cell(cell_node.cell)

    def choose_random_frame_index(self, start=None, end=None) -> int:
        if start is None or start < 0:
            start = 0

        if end is None or end > len(self.frames):
            end = len(self.frames)

        threshold = int(random.random() * self.count_cells_in(start, end))

        for i in range(start, end):
            frame = self.frames[i]
            if len(frame.nodes) > threshold:
                return i
            else:
                threshold -= len(frame.nodes)

        raise RuntimeError('this should not have happened')


class Change:
    @property
    def is_valid(self) -> bool:
        pass

    @property
    def costdiff(self) -> float:
        pass

    def apply(self) -> None:
        pass

class Perturbation(Change):
    def __init__(self, node: CellNodeM, config: Dict[str, Any], realimage, synthimage, cellmap, frame, distmap=None):
        self.node = node
        self.realimage = realimage
        self.synthimage = synthimage
        self.cellmap = cellmap
        self.config = config
        self._checks = []
        self.frame = frame
        cell = node.cell
        new_cell = deepcopy(cell)
        self.replacement_cell = new_cell
        valid = False
        badcount = 0
        self.distmap = distmap
        
        perturb_conf = config["perturbation"]
        p_x = perturb_conf["prob.x"]
        p_y = perturb_conf["prob.y"]
        p_width = perturb_conf["prob.width"]
        p_length = perturb_conf["prob.length"]
        p_rotation = perturb_conf["prob.rotation"]
        
        x_mu = perturb_conf["modification.x.mu"]
        y_mu = perturb_conf["modification.y.mu"]
        width_mu = perturb_conf["modification.width.mu"]
        length_mu = perturb_conf["modification.length.mu"]
        rotation_mu = perturb_conf["modification.rotation.mu"]
        
        x_sigma = perturb_conf["modification.x.sigma"]
        y_sigma = perturb_conf["modification.y.sigma"]
        width_sigma = perturb_conf["modification.width.sigma"]
        length_sigma = perturb_conf["modification.length.sigma"]
        rotation_sigma = perturb_conf["modification.rotation.sigma"]
        
        simulation_config = config["simulation"]
        if simulation_config["image.type"] == "graySynthetic":
            p_opacity = perturb_conf["prob.opacity"]
            opacity_mu = perturb_conf["modification.opacity.mu"]
            opacity_sigma = perturb_conf["modification.opacity.sigma"]
            
        # set starting properties
        if simulation_config["image.type"] == "graySynthetic":
            p_decision = np.array([p_x, p_y, p_width, p_length, p_rotation, p_opacity])
        else:
            p_decision = np.array([p_x, p_y, p_width, p_length, p_rotation])
            
        p = np.random.uniform(0.0, 1.0, size= p_decision.size)
        # generate a sequence such that at least an attribute must be modified
        while not valid and badcount < 50:
            while (p > p_decision).all():
                p = np.random.uniform(0.0, 1.0, size= p_decision.size)
        
            if p[0] < p_decision[0]: #perturb x
                new_cell.x = cell.x + random.gauss(mu=x_mu, sigma=x_sigma)
    
            if p[1] < p_decision[1]: #perturb y
                new_cell.y = cell.y + random.gauss(mu=y_mu, sigma=y_sigma)
        
            if p[2] < p_decision[2]: #perturb width
                new_cell.width = cell.width + random.gauss(mu=width_mu, sigma=width_sigma)
        
            if p[3] < p_decision[3]: #perturb length
                new_cell.length = cell.length + random.gauss(mu=length_mu, sigma=length_sigma)
                
            if p[4] < p_decision[4]: #perturb rotation
                new_cell.rotation = cell.rotation + random.gauss(mu=rotation_mu, sigma=rotation_sigma)
                
            #if simulation_config["image.type"] == "graySynthetic" and p[5] < p_decision[5]:
                #new_cell.opacity = cell.opacity + (random.gauss(mu=opacity_mu, sigma=opacity_sigma))

            # ensure that those changes fall within constraints
            valid = self.is_valid

            if not valid:
                badcount += 1

    @property
    def is_valid(self):
        return check_constraints(self.config, self.realimage.shape, [self.replacement_cell], self.get_checks())

    @property
    def costdiff(self):
        overlap_cost = self.config["overlap.cost"]
        new_synth = self.synthimage.copy()
        new_cellmap = self.cellmap.copy()
        region = self.node.cell.simulated_region(self.frame.simulation_config).\
            union(self.replacement_cell.simulated_region(self.frame.simulation_config))
        self.node.cell.draw(new_synth, new_cellmap, optimization.is_background, self.frame.simulation_config)
        self.replacement_cell.draw(new_synth, new_cellmap, optimization.is_cell, self.frame.simulation_config)

        if useDistanceObjective:
            start_cost = optimization.dist_objective(self.realimage[region.top:region.bottom, region.left:region.right],
                                                     self.synthimage[region.top:region.bottom, region.left:region.right],
                                                     self.distmap[region.top:region.bottom, region.left:region.right],
                                                     self.cellmap[region.top:region.bottom, region.left:region.right],
                                                     overlap_cost)
            end_cost = optimization.dist_objective(self.realimage[region.top:region.bottom, region.left:region.right],
                                                   new_synth[region.top:region.bottom, region.left:region.right],
                                                   self.distmap[region.top:region.bottom, region.left:region.right],
                                                   new_cellmap[region.top:region.bottom, region.left:region.right],
                                                   overlap_cost)
        else:
            start_cost = optimization.objective(self.realimage[region.top:region.bottom, region.left:region.right],
                                self.synthimage[region.top:region.bottom, region.left:region.right],
                                self.cellmap[region.top:region.bottom, region.left:region.right],
                                overlap_cost, self.config["cell.importance"])
            end_cost = optimization.objective(self.realimage[region.top:region.bottom, region.left:region.right],
                              new_synth[region.top:region.bottom, region.left:region.right],
                              new_cellmap[region.top:region.bottom, region.left:region.right],
                              overlap_cost, self.config["cell.importance"])

        return end_cost - start_cost

    def apply(self):
        self.node.cell.draw(self.synthimage, self.cellmap, optimization.is_background, self.frame.simulation_config)
        self.replacement_cell.draw(self.synthimage, self.cellmap, optimization.is_cell, self.frame.simulation_config)
        self.frame.add_cell(self.replacement_cell)

    def get_checks(self) -> List[Tuple[cell.Bacilli, cell.Bacilli]]:
        if not self._checks:
            if self.node.parent:
                if len(self.node.parent.children) == 1:
                    self._checks.append((self.node.parent.cell, self.replacement_cell))
                elif len(self.node.parent.children) == 2:
                    p1, p2 = self.node.parent.cell.split(self.node.cell.split_alpha)

                    if p1.name == self.replacement_cell.name:
                        self._checks.append((p1, self.replacement_cell))
                    elif p2.name == self.replacement_cell.name:
                        self._checks.append((p2, self.replacement_cell))

            if len(self.node.children) == 1:
                self._checks.append((self.replacement_cell, self.node.children[0].cell))
            elif len(self.node.children) == 2:
                p1, p2 = self.replacement_cell.split(self.node.children[0].cell.split_alpha)
                for c in self.node.children:
                    if c.cell.name == p1.name:
                        self._checks.append((p1, c.cell))
                    elif c.cell.name == p2.name:
                        self._checks.append((p2, c.cell))

        return self._checks


class Combination(Change):
    """Move split forward: o<8=8 -> o-o<8"""
    def __init__(self, node: CellNodeM, config, child_realimage, child_synthimage, child_cellmap, child_frame: FrameM, distmap=None):
        self.node = node
        self.config = config
        self.realimage = child_realimage
        self.synthimage = child_synthimage
        self.cellmap = child_cellmap
        self.frame = child_frame
        self._checks = []
        self.combination = None
        self.distmap = distmap

        if len(self.node.children) == 2:
            self.combination = self.node.children[0].cell.combine(self.node.children[1].cell)

    def get_checks(self):
        if self.combination and not self._checks:
            self._checks.append((self.node.cell, self.combination))
            p1, p2 = self.combination.split(self.node.children[0].cell.split_alpha)

            for gc in self.node.grandchildren:
                if gc.cell.name == p1.name:
                    self._checks.append((p1, gc.cell))
                elif gc.cell.name == p2.name:
                    self._checks.append((p2, gc.cell))

        return self._checks

    @property
    def is_valid(self) -> bool:
        return len(self.node.children) == 2 and len(self.node.grandchildren) <= 2 and \
               check_constraints(self.config, self.realimage.shape, [self.combination], self.get_checks())

    @property
    def costdiff(self) -> float:
        overlap_cost = self.config["overlap.cost"]
        new_synth = self.synthimage.copy()
        new_cellmap = self.cellmap.copy()
        region = self.combination.simulated_region(self.frame.simulation_config)

        for child in self.node.children:
            region = region.union(child.cell.simulated_region(self.frame.simulation_config))

        for child in self.node.children:
            child.cell.draw(new_synth, new_cellmap, optimization.is_background, self.frame.simulation_config)

        self.combination.draw(new_synth, new_cellmap, optimization.is_cell, self.frame.simulation_config)

        if useDistanceObjective:
            start_cost = optimization.dist_objective(self.realimage[region.top:region.bottom, region.left:region.right],
                                                     self.synthimage[region.top:region.bottom, region.left:region.right],
                                                     self.distmap[region.top:region.bottom, region.left:region.right],
                                                     self.cellmap[region.top:region.bottom, region.left:region.right],
                                                     overlap_cost)
            end_cost = optimization.dist_objective(self.realimage[region.top:region.bottom, region.left:region.right],
                                                   new_synth[region.top:region.bottom, region.left:region.right],
                                                   self.distmap[region.top:region.bottom, region.left:region.right],
                                                   new_cellmap[region.top:region.bottom, region.left:region.right],
                                                   overlap_cost)
        else:
            start_cost = optimization.objective(self.realimage[region.top:region.bottom, region.left:region.right],
                                self.synthimage[region.top:region.bottom, region.left:region.right],
                                self.cellmap[region.top:region.bottom, region.left:region.right],
                                overlap_cost, self.config["cell.importance"])
            end_cost = optimization.objective(self.realimage[region.top:region.bottom, region.left:region.right],
                              new_synth[region.top:region.bottom, region.left:region.right],
                              new_cellmap[region.top:region.bottom, region.left:region.right],
                              overlap_cost, self.config["cell.importance"])

        return end_cost - start_cost - self.config["split.cost"]

    def apply(self) -> None:
        self.combination.draw(self.synthimage, self.cellmap, optimization.is_cell, self.frame.simulation_config)
        grandchildren = self.node.grandchildren

        for child in self.node.children:
            del self.frame.node_map[child.cell.name]
            child.cell.draw(self.synthimage, self.cellmap, optimization.is_background, self.frame.simulation_config)

        self.node.children = []
        combination_node = self.node.make_child(self.combination)
        self.frame.node_map[self.combination.name] = combination_node

        for gc in grandchildren:
            combination_node.children.append(gc)
            gc.parent = combination_node


class Split(Change):
    """Move split backward: o-o<8 -> o<8=8"""
    def __init__(self, node: CellNodeM, config, child_realimage, child_synthimage, child_cellmap, child_frame: FrameM, distmap=None):
        self.node = node
        self.config = config
        self.realimage = child_realimage
        self.synthimage = child_synthimage
        self.cellmap = child_cellmap
        self.frame = child_frame
        self._checks = []
        self.s1 = self.s2 = None
        self.distmap = distmap

        if len(self.node.children) == 1:
            alpha = random.random()/5 + 2/5
            self.s1, self.s2 = self.node.children[0].cell.split(alpha)

    def get_checks(self):
        if len(self.node.children) == 1 and not self._checks:
            p1, p2 = self.node.cell.split(self.s1.split_alpha)

            if p1.name == self.s1.name:
                self._checks.append((p1, self.s1))
            elif p1.name == self.s2.name:
                self._checks.append((p1, self.s2))

            if p2.name == self.s1.name:
                self._checks.append((p2, self.s1))
            elif p2.name == self.s2.name:
                self._checks.append((p2, self.s2))

            for child in self.node.grandchildren:
                if child.cell.name == self.s1.name:
                    self._checks.append((self.s1, child.cell))
                elif child.cell.name == self.s2.name:
                    self._checks.append((self.s2, child.cell))

        return self._checks

    @property
    def is_valid(self) -> bool:
        return len(self.node.children) == 1 and len(self.node.grandchildren) != 1 and \
               check_constraints(self.config, self.realimage.shape, [self.s1, self.s2], self.get_checks())

    @property
    def costdiff(self) -> float:
        overlap_cost = self.config["overlap.cost"]
        new_synth = self.synthimage.copy()
        new_cellmap = self.cellmap.copy()
        region = self.node.children[0].cell.simulated_region(self.frame.simulation_config).\
            union(self.s1.simulated_region(self.frame.simulation_config)).\
                union(self.s2.simulated_region(self.frame.simulation_config))
        self.node.children[0].cell.draw(new_synth, new_cellmap, optimization.is_background, self.frame.simulation_config)
        self.s1.draw(new_synth, new_cellmap, optimization.is_cell, self.frame.simulation_config)
        self.s2.draw(new_synth, new_cellmap, optimization.is_cell, self.frame.simulation_config)

        if useDistanceObjective:
            start_cost = optimization.dist_objective(self.realimage[region.top:region.bottom, region.left:region.right],
                                                     self.synthimage[region.top:region.bottom, region.left:region.right],
                                                     self.distmap[region.top:region.bottom, region.left:region.right],
                                                     self.cellmap[region.top:region.bottom, region.left:region.right],
                                                     overlap_cost)
            end_cost = optimization.dist_objective(self.realimage[region.top:region.bottom, region.left:region.right],
                                                   new_synth[region.top:region.bottom, region.left:region.right],
                                                   self.distmap[region.top:region.bottom, region.left:region.right],
                                                   new_cellmap[region.top:region.bottom, region.left:region.right],
                                                   overlap_cost)
        else:
            start_cost = optimization.objective(self.realimage[region.top:region.bottom, region.left:region.right],
                                self.synthimage[region.top:region.bottom, region.left:region.right],
                                self.cellmap[region.top:region.bottom, region.left:region.right],
                                overlap_cost, self.config["cell.importance"])
            end_cost = optimization.objective(self.realimage[region.top:region.bottom, region.left:region.right],
                              new_synth[region.top:region.bottom, region.left:region.right],
                              new_cellmap[region.top:region.bottom, region.left:region.right],
                              overlap_cost, self.config["cell.importance"])

        return end_cost - start_cost + self.config["split.cost"]

    def apply(self) -> None:
        self.node.children[0].cell.draw(self.synthimage, self.cellmap, optimization.is_background, self.frame.simulation_config)
        self.s1.draw(self.synthimage, self.cellmap, optimization.is_cell, self.frame.simulation_config)
        self.s2.draw(self.synthimage, self.cellmap, optimization.is_cell, self.frame.simulation_config)
        del self.frame.node_map[self.node.children[0].cell.name]
        grandchildren = self.node.grandchildren
        self.node.children = []
        s1_node = self.node.make_child(self.s1)
        s2_node = self.node.make_child(self.s2)
        self.frame.node_map[self.s1.name] = s1_node
        self.frame.node_map[self.s2.name] = s2_node

        for gc in grandchildren:
            if gc.cell.name == self.s1.name:
                gc.parent = s1_node
                s1_node.children = [gc]
            elif gc.cell.name == self.s2.name:
                gc.parent = s2_node
                s2_node.children = [gc]

class BackGround_luminosity_offset(Change):
    def __init__(self, frame, realimage, synthimage, cellmap, config, distmap=None):
        self.frame = frame
        self.realimage = realimage
        self.old_synthimage = synthimage
        self.cellmap = cellmap
        self.old_simulation_config = frame.simulation_config
        self.new_simulation_config = frame.simulation_config.copy()
        self.config = config
        
        offset_mu = config["perturbation"]["modification.background_offset.mu"]
        offset_sigma = config["perturbation"]["modification.background_offset.sigma"]
        self.new_simulation_config["background.color"] += random.gauss(mu=offset_mu, sigma=offset_sigma)
        self.new_synthimage, _ = optimization.generate_synthetic_image(frame.nodes, realimage.shape, self.new_simulation_config)
        
    @property
    def is_valid(self) -> bool:
        return self.frame.simulation_config["background.color"] > 0
    @property
    def costdiff(self) -> float:
        overlap_cost = self.config["overlap.cost"]
        if useDistanceObjective:
            start_cost = optimization.dist_objective(self.realimage,self.old_synthimage,self.distmap,self.cellmap,overlap_cost)
            end_cost = optimization.dist_objective(self.realimage,self.new_synthimage,self.distmap,self.cellmap,overlap_cost)
        else:
            start_cost = optimization.objective(self.realimage,self.old_synthimage,self.cellmap,overlap_cost, self.config["cell.importance"])
            end_cost = optimization.objective(self.realimage,self.new_synthimage,self.cellmap,overlap_cost, self.config["cell.importance"])
        return end_cost-start_cost
    
    def apply(self):
        self.old_synthimage[:]= self.new_synthimage
        self.old_simulation_config["background.color"] = self.new_simulation_config["background.color"]
        
def save_output(imagefiles, realimages, synthimages, cellmaps, lineage: LineageM, args, lineagefile, config):
    for frame_index in range(len(lineage.frames)):
        realimage = realimages[frame_index]
        cellnodes = lineage.frames[frame_index].nodes
        cellmap = cellmaps[frame_index]
        synthimage = synthimages[frame_index]
        cost = optimization.objective(realimage, synthimage, cellmap, config["overlap.cost"], config["cell.importance"])
        print('Final Cost:', cost)
        for node in cellnodes:
            properties = [imagefiles[frame_index].name, node.cell.name]
            properties.extend([
                str(node.cell.x),
                str(node.cell.y),
                str(node.cell.width),
                str(node.cell.length),
                str(node.cell.rotation)])
            print(','.join(properties), file=lineagefile)

def build_initial_lineage(imagefiles, lineageframes, args, config):
    lineage = LineageM(config["simulation"])

    colony = lineageframes.latest
    for cellnode in colony:
        lineage.frames[0].add_cell(cellnode.cell)

    return lineage


def gerp(a, b, t):
    """Geometric interpolation"""
    return a * (b / a) ** t


def optimize(imagefiles, lineageframes, lineagefile, args, config, iteration_per_cell=6000, in_auto_temp_schedule=False, const_temp=None):
    global useDistanceObjective
    useDistanceObjective = args.dist
    
    if not args.output.is_dir():
        args.output.mkdir()
    if not args.bestfit.is_dir():
        args.bestfit.mkdir()
    if args.residual and not args.output.is_dir():
        args.residual.mkdir()
        
    lineage = build_initial_lineage(imagefiles, lineageframes, args, config)
    realimages = [optimization.load_image(imagefile) for imagefile in imagefiles]
    shape = realimages[0].shape
    synthimages = []
    cellmaps = []
    distmaps = []
    pbad_total = 0
    window = config["global_optimizer.window_size"]
    perturbation_prob = config["prob.perturbation"]
    combine_prob = config["prob.combine"]
    split_prob = config["prob.split"]
    background_offset_prob = config["perturbation"]["prob.background_offset"]
    residual_vmin = config["residual.vmin"]
    residual_vmax = config["residual.vmax"]
    if args.residual:
        colormap = cm.ScalarMappable(norm = Normalize(vmin=residual_vmin, vmax=residual_vmax), cmap = "bwr")
    if not useDistanceObjective:
        distmaps = [None] * len(realimages)

    for window_start in range(1 - window, len(realimages)):
        window_end = window_start + window
        print(window_start, window_end)

        if window_end <= len(realimages):
            # get initial estimate
            if window_end > 1:
                lineage.copy_forward()

            # add next diffimage
            realimage = realimages[window_end - 1]
            synthimage, cellmap = optimization.generate_synthetic_image(lineage.frames[-1].nodes, shape, lineage.frames[-1].simulation_config)
            synthimages.append(synthimage)
            cellmaps.append(cellmap)
            if useDistanceObjective:
                distmap = distance_transform_edt(realimage < .5)
                distmap /= config[f'{config["global.cellType"].lower()}.distanceCostDivisor'] * config[
                    'global.pixelsPerMicron']
                distmap += 1
                distmaps.append(distmap)

        # simulated annealing
        run_count = iteration_per_cell*lineage.count_cells_in(window_start, window_end)//window
        print(run_count)
        bad_count = 0
        for iteration in range(run_count):
            frame_index = lineage.choose_random_frame_index(window_start, window_end)
            if in_auto_temp_schedule:
                temperature = const_temp
            else:
                frame_start_temp = gerp(args.end_temp, args.start_temp, (frame_index - window_start + 1)/window)
                frame_end_temp = gerp(args.end_temp, args.start_temp, (frame_index - window_start)/window)
                temperature = gerp(frame_start_temp, frame_end_temp, iteration/(run_count - 1))                
            frame = lineage.frames[frame_index]
            node = random.choice(frame.nodes)
            change_option = np.random.choice(["split", "perturbation", "combine", "background_offset"], p=[split_prob, perturbation_prob, combine_prob, background_offset_prob])
            change = None
            if change_option == "split" and random.random() < optimization.split_proba(node.cell.length) and frame_index > 0:
                change = Split(node.parent, config, realimages[frame_index], synthimages[frame_index], cellmaps[frame_index], lineage.frames[frame_index], distmaps[frame_index])
                
            elif change_option == "perturbation":
                change = Perturbation(node, config, realimages[frame_index], synthimages[frame_index], cellmaps[frame_index], lineage.frames[frame_index], distmaps[frame_index])
                
            elif change_option == "combine" and frame_index > 0:
                change = Combination(node.parent, config, realimages[frame_index], synthimages[frame_index], cellmaps[frame_index], lineage.frames[frame_index], distmaps[frame_index])

            elif change_option == "background_offset" and frame_index > 0 and config["simulation"]["image.type"] == "graySynthetic":
                change = BackGround_luminosity_offset(lineage.frames[frame_index], realimages[frame_index], synthimages[frame_index], cellmaps[frame_index], config)
            
            if change and change.is_valid:
                # apply if acceptable
                costdiff = change.costdiff
    
                if costdiff <= 0:
                    acceptance = 1.0
                else:
                    bad_count += 1
                    acceptance = np.exp(-costdiff / temperature)
                    pbad_total += acceptance
    
                if acceptance > random.random():
                    change.apply()
        if in_auto_temp_schedule:
           print("pbad is ", pbad_total/bad_count)
           return pbad_total/bad_count
       
        #output module
        if window_start >= 0:
            bestfit_frame = Image.fromarray(np.uint8(255*synthimages[window_start]), "L")
            bestfit_frame.save(args.bestfit / imagefiles[window_start].name)
            
            output_frame = np.empty((realimages[frame_index].shape[0], realimages[frame_index].shape[1], 3))
            output_frame[..., 0] = realimages[frame_index]
            output_frame[..., 1] = output_frame[..., 0]
            output_frame[..., 2] = output_frame[..., 0]
            for node in lineage.frames[frame_index].nodes:
                node.cell.drawoutline(output_frame, (1, 0, 0))
            output_frame = Image.fromarray(np.uint8(255*output_frame))
            output_frame.save(args.output / imagefiles[window_start].name)
                
            if args.residual:
                residual_frame = Image.fromarray(np.uint8(255*colormap.to_rgba(np.clip(realimages[window_start] - synthimages[window_start],
                                                                                       residual_vmin, residual_vmax))), "RGBA")
                residual_frame.save(args.residual / imagefiles[window_start].name)

    save_output(imagefiles, realimages, synthimages, cellmaps, lineage, args, lineagefile, config)

def auto_temp_schedule(imagefiles, lineageframes, lineagefile, args, config):
    initial_temp = 1
    iteration_per_cell = 500
    count=0
    window_imagefiles = imagefiles[:config["global_optimizer.window_size"]]
    
    while(optimize(window_imagefiles, lineageframes, lineagefile, args, config, iteration_per_cell, 
                   in_auto_temp_schedule=True, const_temp=initial_temp)<0.40):
        count += 1
        initial_temp *= 10.0
        print(f"count: {count}")
    print("finished < 0.4")
    while(optimize(window_imagefiles, lineageframes, lineagefile, args, config, iteration_per_cell, 
                   in_auto_temp_schedule=True, const_temp=initial_temp)>0.40):
        count += 1 
        initial_temp /= 10.0
        print(f"count: {count}")
    print("finished > 0.4")
    while(optimize(window_imagefiles, lineageframes, lineagefile, args, config, iteration_per_cell, 
                   in_auto_temp_schedule=True, const_temp=initial_temp)<0.40):
        count += 1
        initial_temp *= 1.1
        print(f"count: {count}")
    end_temp = initial_temp
    print("finished < 0.4")
    while(optimize(window_imagefiles, lineageframes, lineagefile, args, config, iteration_per_cell, 
                   in_auto_temp_schedule=True, const_temp=end_temp)>1e-10):
        count += 1
        end_temp /= 10.0

    return initial_temp, end_temp
    