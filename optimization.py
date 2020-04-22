import random
import time
import math
from math import sqrt, atan
from itertools import chain

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import matplotlib.pyplot as plt
from scipy.ndimage import distance_transform_edt

from cell import Bacilli
from colony import CellNode, Colony

import dask.distributed

FONT = ImageFont.load_default()

debugcount = 0
badcount = 0  # DEBUG
is_cell = True
is_background = False



def objective(realimage, synthimage):
    """Full objective function between two images."""
    return np.sum((realimage - synthimage)**2)


def dist_objective(diffimage, distmap):
    return np.sum((diffimage*distmap)**2)


def generate_synthetic_image(cellnodes, shape, graySyntheticImage, phaseContractImage=None):

    if graySyntheticImage:
        synthimage = np.full(shape, 0.39)  # pixel value: 0.39*255 == 100
        for node in cellnodes:
            node.cell.draw(synthimage, is_cell, graySyntheticImage, phaseContractImage)  # pixel value: 0.15*255 == 40
        return synthimage
    else:
        synthimage = np.zeros(shape)
        for node in cellnodes:
            node.cell.draw(synthimage, is_cell, graySyntheticImage, phaseContractImage)
        return synthimage



def load_image(imagefile):
    """Open the image file and convert to a floating-point grayscale array."""
    with open(imagefile, 'rb') as fp:
        realimage = np.array(Image.open(fp))
    if len(realimage.shape) > 2 or realimage.dtype != np.uint8:
        raise ValueError(f'Expects 8-bit grayscale images: "{imagefile}"')
    return realimage.astype(np.float)/255


def perturb_bacilli(node, config, imageshape):
    """Create a new perturbed bacilli cell."""
    global badcount  # DEBUG
    cell = node.cell
    prior = node.prior.cell

    if node.split:
        p1, p2 = node.prior.cell.split(node.alpha)
        if p1.name == node.cell.name:
            prior = p1
        elif p2.name == node.cell.name:
            prior = p2
        else:
            AssertionError('Names not matching')

    max_displacement = config['bacilli.maxSpeed']/config['global.framesPerSecond']
    max_rotation = config['bacilli.maxSpin']/config['global.framesPerSecond']
    min_growth = config['bacilli.minGrowth']
    max_growth = config['bacilli.maxGrowth']
    min_width = config['bacilli.minWidth']
    max_width = config['bacilli.maxWidth']
    min_length = config['bacilli.minLength']
    max_length = config['bacilli.maxLength']

    # set starting properties
    x = cell.x
    y = cell.y
    width = cell.width
    length = cell.length
    rotation = cell.rotation

    modified = False
    wasbad = False  # DEBUG
    while not modified:

        # randomly make changes
        if random.random() < 0.35:
            x = cell.x + random.gauss(mu=0, sigma=0.5)
            modified = True

        if random.random() < 0.35:
            y = cell.y + random.gauss(mu=0, sigma=0.5)
            modified = True

        if random.random() < 0.1:
            width = cell.width + random.gauss(mu=0, sigma=0.1)
            modified = True

        if random.random() < 0.2:
            length = cell.length + random.gauss(mu=0, sigma=1)
            modified = True

        if random.random() < 0.2:
            rotation = cell.rotation + random.gauss(mu=0, sigma=0.2)
            modified = True

        # ensure that those changes fall within constraints
        if modified:
            displacement = sqrt(np.sum((np.array([x, y, 0] - prior.position))**2))
            bad = False
            if not (0 <= x < imageshape[1] and 0 <= y < imageshape[0]):
                bad = True
            elif displacement > max_displacement:
                bad = True
            elif width < min_width or width > max_width:
                bad = True
            elif abs(rotation - prior.rotation) > max_rotation:
                bad = True
            elif not (min_length < length < max_length):
                bad = True
            elif not (min_growth < length - prior.length < max_growth):
                bad = True
            if bad:
                wasbad = True   # DEBUG
                x = cell.x
                y = cell.y
                width = cell.width
                length = cell.length
                rotation = cell.rotation
                modified = False

    if wasbad:  # DEBUG
        badcount += 1

    # push the new cell over the previous in the node
    node.push(Bacilli(cell.name, x, y, width, length, rotation))

def split_proba(length):
    """Returns the split probability given the length of the cell."""
    # Determined empirically based on previous runs
    return math.sin((length - 14) / (2 * math.pi * math.pi)) if 14 <= length <= 45 else 0

def bacilli_split(node, config, imageshape):
    """Split the cell and push both onto the stack for testing."""

    if random.random() < split_proba(node.cell.length):
        return False

    max_displacement = config['bacilli.maxSpeed']/config['global.framesPerSecond']
    max_rotation = config['bacilli.maxSpin']/config['global.framesPerSecond']
    min_width = config['bacilli.minWidth']
    max_width = config['bacilli.maxWidth']
    min_length = config['bacilli.minLength']
    max_length = config['bacilli.maxLength']

    alpha = random.random()/5 + 2/5     # TODO choose from config
    cell1, cell2 = node.cell.split(alpha)

    # make sure that the lengths are within constraints
    if not (min_length < cell1.length < max_length and
            min_length < cell2.length < max_length):
        return False

    # split the prior to compare with for ensuring constraints are met
    pcell1, pcell2 = node.prior.cell.split(alpha)

    displacement = sqrt(np.sum((cell1.position - pcell1.position)**2))
    if not (0 <= cell1.position.x < imageshape[1] and
            0 <= cell1.position.y < imageshape[0]):
        return False
    elif displacement > max_displacement:
        return False
    elif not (min_width < cell1.width < max_width):
        return False
    elif abs(cell1.rotation - pcell1.rotation) > max_rotation:
        return False
    elif not (min_length < cell1.length < max_length):
        return False

    displacement = sqrt(np.sum((cell2.position - pcell2.position)**2))
    if not (0 <= cell2.position.x < imageshape[1] and
            0 <= cell2.position.y < imageshape[0]):
        return False
    elif displacement > max_displacement:
        return False
    elif not (min_width < cell2.width < max_width):
        return False
    elif abs(cell2.rotation - pcell2.rotation) > max_rotation:
        return False
    elif not (min_length < cell2.length < max_length):
        return False

    # push the split to the top of the cell stack
    node.parent.pop()
    node.parent.push2(cell1, cell2, alpha)

    return True


def bacilli_combine(node, config, imageshape):
    """Split the cell and push both onto the stack for testing."""
    if random.random() > 0.2:
        return False, None

    max_displacement = config['bacilli.maxSpeed']/config['global.framesPerSecond']
    max_rotation = config['bacilli.maxSpin']/config['global.framesPerSecond']
    min_width = config['bacilli.minWidth']
    max_width = config['bacilli.maxWidth']
    min_length = config['bacilli.minLength']
    max_length = config['bacilli.maxLength']

    # get the cell node right before the split
    presplit = node.parent
    while len(presplit.children) < 2:
        presplit = presplit.parent

    # get the latest cell nodes after the split
    top_node1, top_node2 = presplit.children
    while top_node1.children:
        top_node1 = top_node1.children[0]
    while top_node2.children:
        top_node2 = top_node2.children[0]

    # combine the cells
    new_cell = top_node1.cell.combine(top_node2.cell)

    # compare with the prior for constraint checking
    prior_cell = presplit.prior.cell
    displacement = sqrt(np.sum((new_cell.position - new_cell.position)**2))
    if not (0 <= new_cell.position.x < imageshape[1] and
            0 <= new_cell.position.y < imageshape[0]):
        return False, None
    elif displacement > max_displacement:
        return False, None
    elif not (min_width < new_cell.width < max_width):
        return False, None
    elif abs(new_cell.rotation - prior_cell.rotation) > max_rotation:
        return False, None
    elif not (min_length < new_cell.length < max_length):
        return False, None

    # HACK
    # The following is somewhat of a work-around needed because the combined
    # cell cannot be pushed on top of split cells; therefore the previous
    # split cells will actually be on top instead of below the combined.

    # Here, push the new combined cell on top of the cell before the split;
    # then push the original two cells on top of the combined cell. The
    # stack will be treated differently after this point to account for this.
    presplit.pop()
    presplit.push(new_cell)
    new_node = presplit.children[0]
    if top_node1.cell.name == node.cell.name:
        new_node.push2(node.parent.cell, top_node2.cell, node.alpha)
        # new_node.children[0].push(top_node1.cell)
    elif top_node2.cell.name == node.cell.name:
        new_node.push2(top_node1.cell, node.parent.cell, node.alpha)
        # new_node.children[1].push(top_node2.cell)

    return True, presplit

# functions for different types of dynamic auto-temperature scheduling
def auto_temp_schedule_frame(frame, k_frame):
    return frame % k_frame == 0

def auto_temp_schedule_factor(cell_num, prev_num, factor):
    return True if cell_num / prev_num >= factor else False

def auto_temp_schedule_const(cell_num, prev_num, constant):
    return True if cell_num - prev_num >= constant else False

def optimize_core(imagefile, colony, args, config, iterations_per_cell=2000, auto_temp_complete=True, auto_const_temp = 1):
    """Core of the optimization routine."""
    global debugcount, badcount  # DEBUG

    bad_count = 0
    bad_prob_tot = 0

    realimage = load_image(imagefile)
    shape = realimage.shape

    celltype = config['global.cellType'].lower()
    useDistanceObjective = args.dist

    cellnodes = list(colony)

    # find the initial cost
    synthimage = generate_synthetic_image(cellnodes, realimage.shape, args.graysynthetic, args.phaseContractImage)
    diffimage = realimage - synthimage
    if useDistanceObjective:
        distmap = distance_transform_edt(realimage < .5)
        distmap /= config[f'{celltype}.distanceCostDivisor']*config['global.pixelsPerMicron']
        distmap += 1
        cost = dist_objective(diffimage, distmap)
    else:
        cost = objective(realimage, synthimage)

    # setup temperature schedule
    run_count = int(iterations_per_cell*len(cellnodes))

    if (auto_temp_complete == False):
        temperature = auto_const_temp
    else:
        temperature = args.start_temp
        end_temperature = args.end_temp

        alpha = (end_temperature/temperature)**(1/run_count)

    for i in range(run_count):
        # print progress for debugging purposes
        #if i%1013 == 59:
        #    print(f'{imagefile.name}: Progress: {100*i/run_count:.02f}%', flush=True)

        # choose a cell at random
        index = random.randint(0, len(cellnodes) - 1)
        node = cellnodes[index]

        # perturb the cell and push it onto the stack
        if celltype == 'bacilli':
            perturb_bacilli(node, config, shape)
            new_node = node.children[0]

            old_diff = diffimage.copy()

            # # try splitting (or combining if already split)
            combined = False
            split = False
            if node.split:
                combined, presplit = bacilli_combine(new_node, config, shape)
            elif not node.split:
                split = bacilli_split(new_node, config, shape)

            if combined:
                # get the new combined node
                cnode = presplit.children[0]

                # get the previous split nodes (see note in bacilli_combine)
                snode1, snode2 = cnode.children

                # compute the starting cost
                region = (cnode.cell.region.union(snode1.cell.region)
                                           .union(snode2.cell.region))
                if useDistanceObjective:
                    start_cost = dist_objective(
                        diffimage[region.top:region.bottom, region.left:region.right],
                        distmap[region.top:region.bottom, region.left:region.right])
                else:
                    start_cost = np.sum(diffimage[region.top:region.bottom,
                                                region.left:region.right]**2)

                # subtract the previous cells
                snode1.cell.draw(diffimage, is_cell, args.graysynthetic, args.phaseContractImage)
                snode2.cell.draw(diffimage, is_cell, args.graysynthetic, args.phaseContractImage)

                # add the new cell
                cnode.cell.draw(diffimage, is_background, args.graysynthetic, args.phaseContractImage)

            elif split:
                snode1, snode2 = node.children

                # compute the starting cost
                region = (node.cell.region.union(snode1.cell.region)
                                          .union(snode2.cell.region))
                if useDistanceObjective:
                    start_cost = dist_objective(
                        diffimage[region.top:region.bottom, region.left:region.right],
                        distmap[region.top:region.bottom, region.left:region.right])
                else:
                    start_cost = np.sum(diffimage[region.top:region.bottom,
                                                region.left:region.right]**2)
                
                # subtract the previous cell
                node.cell.draw(diffimage, is_cell, args.graysynthetic, args.phaseContractImage)

                # add the new cells
                snode1.cell.draw(diffimage, is_background, args.graysynthetic, args.phaseContractImage)
                snode2.cell.draw(diffimage, is_background, args.graysynthetic, args.phaseContractImage)

            else:
                # compute the starting cost
                region = node.cell.region.union(new_node.cell.region)
                if useDistanceObjective:
                    start_cost = dist_objective(
                        diffimage[region.top:region.bottom, region.left:region.right],
                        distmap[region.top:region.bottom, region.left:region.right])
                else:
                    start_cost = np.sum(diffimage[region.top:region.bottom,
                                                region.left:region.right]**2)

                # subtract the previous cell
                node.cell.draw(diffimage, is_cell, args.graysynthetic, args.phaseContractImage)

                # add the new cells
                new_node.cell.draw(diffimage, is_background, args.graysynthetic, args.phaseContractImage)

            # compute the cost difference
            if useDistanceObjective:
                end_cost = dist_objective(
                    diffimage[region.top:region.bottom, region.left:region.right],
                    distmap[region.top:region.bottom, region.left:region.right])
            else:
                end_cost = np.sum(diffimage[region.top:region.bottom,
                                            region.left:region.right]**2)
            costdiff = end_cost - start_cost

            # compute the acceptance threshold
            if costdiff <= 0:
                acceptance = 1.0
            else:
                acceptance = np.exp(-costdiff/temperature)
                bad_count += 1
                bad_prob_tot += acceptance

            # check if the acceptance threshold was met; pop if not
            if acceptance <= random.random():
                # restore the previous cells
                if combined:
                    presplit.pop()
                    presplit.push2(snode1.cell, snode2.cell, node.alpha)
                else:
                    node.pop()

                # restore the diff image
                diffimage = old_diff

            else:
                if combined:
                    presplit.children[0].pop()

                cost += costdiff

            colony.flatten()
            cellnodes = list(colony)


            # DEBUG
            if args.debug and i%80 == 0:
                synthimage = generate_synthetic_image(cellnodes, realimage.shape, args.graysynthetic, args.phaseContractImage)

                frame_1 = np.empty((shape[0], shape[1], 3))
                frame_1[..., 0] = (realimage - synthimage)
                frame_1[..., 1] = frame_1[..., 0]
                frame_1[..., 2] = frame_1[..., 0]

                #for node in cellnodes:
                    #node.cell.drawoutline(frame_1, (1, 0, 0))

                frame_1 = np.clip(frame_1, 0, 1)

                debugimage = Image.fromarray((255*frame_1).astype(np.uint8))
                debugimage.save(args.debug/f'residual{debugcount}.png')


                frame_2 = np.empty((shape[0], shape[1], 3))
                frame_2[..., 0] = synthimage
                frame_2[..., 1] = frame_2[..., 0]
                frame_2[..., 2] = frame_2[..., 0]

                #for node in cellnodes:
                    #node.cell.drawoutline(frame_2, (1, 0, 0))

                frame_2 = np.clip(frame_2, 0, 1)

                debugimage = Image.fromarray((255*frame_2).astype(np.uint8))
                debugimage.save(args.debug/f'synthetic{debugcount}.png')
                debugcount += 1

        if (auto_temp_complete == True):
            temperature *= alpha

    # print(f'Bad Percentage: {100*badcount/run_count}%')

    if (auto_temp_complete == False):

        # print("pbad is ", bad_prob_tot/bad_count)
        # print("temperature is ", temperature)

        return bad_prob_tot/bad_count

    synthimage = generate_synthetic_image(cellnodes, realimage.shape, args.graysynthetic, args.phaseContractImage)
    if useDistanceObjective:
        new_cost = dist_objective(realimage - synthimage, distmap)
    else:
        new_cost = objective(realimage, synthimage)
    print(f'Incremental Cost: {cost}')
    print(f'Actual Cost:      {new_cost}')
    if abs(new_cost - cost) > 1e-7:
        print('WARNING: incremental cost diverged from expected cost')

    frame = np.empty((shape[0], shape[1], 3))
    frame[..., 0] = realimage
    frame[..., 1] = frame[..., 0]
    frame[..., 2] = frame[..., 0]

    for node in cellnodes:
        node.cell.drawoutline(frame, (1, 0, 0))

    frame = np.clip(frame, 0, 1)

    debugimage = Image.fromarray((255*frame).astype(np.uint8))

    return colony, cost, debugimage

def auto_temp_schedule(imagefile, colony, args, config):
    initial_temp = 1
    ITERATION = 500
    AUTO_TEMP_COMPLETE = False

    while(optimize_core(imagefile, colony, args, config, ITERATION, AUTO_TEMP_COMPLETE, initial_temp)<0.40):
        initial_temp *= 10.0
    while(optimize_core(imagefile, colony, args, config, ITERATION, AUTO_TEMP_COMPLETE, initial_temp)>0.40):
        initial_temp /= 10.0
    while(optimize_core(imagefile, colony, args, config, ITERATION, AUTO_TEMP_COMPLETE, initial_temp)<0.40):
        initial_temp *= 1.1

    end_temp = initial_temp

    while(optimize_core(imagefile, colony, args, config, ITERATION, AUTO_TEMP_COMPLETE, end_temp)>=1e-10):
        end_temp /= 10.0

    return initial_temp, end_temp

def optimize(imagefile, lineageframes, args, config, client):
    """Optimize the cell properties using simulated annealing."""
    global badcount  # DEBUG
    badcount = 0  # DEBUG

    if not client:
        colony,_,debugimage = optimize_core(imagefile, lineageframes.forward(), args, config)
        debugimage.save(args.output / imagefile.name)
        return colony

    #tasks = []

    group = lineageframes.latest_group
    ejob = args.jobs // len(group)

    futures = []

    for colony in group:
        for i in range(ejob):
            newColony = colony.clone()
            futures.append(client.submit(optimize_core, imagefile, newColony, args, config))

    try:
        dask.distributed.wait(futures, 360)
    except Exception as e:
        print(e)

    results = []
    for future in futures:
        if not future.done():
            print('Task timed out - Cancelling')
            future.cancel()
        else:
            results.append(future.result(timeout=10))

    if args.strategy not in ['best-wins', 'worst-wins', 'extreme-wins']:
        raise ValueError('--strategy must be one of "best-wins", "worst-wins", "extreme-wins"')

    if args.strategy in ['best-wins', 'worst-wins']:
        results = sorted(results, key=lambda x: x[1], reverse=args.strategy == 'worst-wins')
    else:
        bestresults = sorted(results, key=lambda x: x[1])
        worstresults = sorted(results, key=lambda x: x[1], reverse=True)
        # https://stackoverflow.com/a/11125256
        results = list(chain.from_iterable(zip(bestresults, worstresults)))

    winning = results[:args.keep]
    print('keeping {}, got {}'.format(args.keep, len(winning)))

    # Choose the best or worst
    print('The winning solution(s) ({}) have cost values {}'.format(args.strategy, [s[1] for s in winning]))
    print('CHECKPOINT, {}, {}, {}'.format(time.time(), imagefile.name, winning[0][1]), flush=True)

    lineageframes.add_frame([s[0] for s in winning])

    for i, s in enumerate(winning):
        debugimage = s[2]
        debugimage.save(args.output/'{:03d}-{}'.format(i, imagefile.name))

    return colony
