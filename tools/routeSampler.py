#!/usr/bin/env python
# Eclipse SUMO, Simulation of Urban MObility; see https://eclipse.org/sumo
# Copyright (C) 2012-2020 German Aerospace Center (DLR) and others.
# This program and the accompanying materials are made available under the
# terms of the Eclipse Public License 2.0 which is available at
# https://www.eclipse.org/legal/epl-2.0/
# This Source Code may also be made available under the following Secondary
# Licenses when the conditions for such availability set forth in the Eclipse
# Public License 2.0 are satisfied: GNU General Public License, version 2
# or later which is available at
# https://www.gnu.org/licenses/old-licenses/gpl-2.0-standalone.html
# SPDX-License-Identifier: EPL-2.0 OR GPL-2.0-or-later

# @file    routeSampler.py
# @author  Jakob Erdmann
# @date    2020-02-07

"""
Samples routes from a given set to fullfill specified counting data (edge counts or turn counts)
"""
from __future__ import absolute_import
from __future__ import print_function

import os
import sys
import random
from argparse import ArgumentParser

if 'SUMO_HOME' in os.environ:
    sys.path.append(os.path.join(os.environ['SUMO_HOME'], 'tools'))
import sumolib  # noqa


def get_options(args=None):
    parser = ArgumentParser(description="Sample routes to match counts")
    parser.add_argument("-t", "--turn-file", dest="turnFile",
                        help="Input turn-count file")
    parser.add_argument("-d", "--edgedata-file", dest="edgeDataFile",
                        help="Input edgeData file file (for counts)")
    parser.add_argument("--edgedata-attribute", dest="edgeDataAttr", default="entered",
                        help="Read edgeData counts from the given attribute")
    parser.add_argument("--turn-attribute", dest="turnAttr", default="probability",
                        help="Read turning counts from the given attribute")
    parser.add_argument("-r", "--route-file", dest="routeFile",
                        help="Input route file file")
    parser.add_argument("-o", "--output-file", dest="out", default="out.rou.xml",
                        help="Output route file")
    parser.add_argument("--prefix", dest="prefix", default="",
                        help="prefix for the vehicle ids")
    parser.add_argument("-a", "--attributes", dest="vehattrs", default="",
                        help="additional vehicle attributes")
    parser.add_argument("-s", "--seed", type=int, default=42,
                        help="random seed")
    parser.add_argument("--deficit-output", dest="deficitOut",
                        help="write edge-data with deficit information to FILE")
    parser.add_argument("--optimize",
                        help="set optimization method level (full, INT boundary)")
    parser.add_argument("--optimize-input", dest="optimizeInput", action="store_true", default=False,
                        help="Skip resampling and run optimize directly on the input routes")
    parser.add_argument("-v", "--verbose", action="store_true", default=False,
                        help="tell me what you are doing")

    options = parser.parse_args(args=args)
    if (options.routeFile is None or
            (options.turnFile is None and options.edgeDataFile is None)):
        parser.print_help()
        sys.exit()
    options.turnFile = options.turnFile.split(',') if options.turnFile is not None else []
    options.edgeDataFile = options.edgeDataFile.split(',') if options.edgeDataFile is not None else []
    if options.vehattrs and options.vehattrs[0] != ' ':
        options.vehattrs = ' ' + options.vehattrs

    if options.optimize is not None:
        try:
            import scipy.optimize
            if options.optimize != "full":
                try:
                    options.optimize = int(options.optimize)
                except:
                    print("Option optimize requires the value 'full' or an integer", file=sys.stderr)
                    sys.exit(1)
        except ImportError:
            print("Cannot use optimization (scipy not installed)", file=sys.stderr)
            sys.exit(1)

    if options.optimizeInput and type(options.optimize) != int:
        print("Option --optimize-input requires an integer argument for --optimize", file=sys.stderr)
        sys.exit(1)

    return options


class CountData:
    def __init__(self, count, edgeTuple, allRoutes):
        self.origCount = count
        self.count = count
        self.edgeTuple = edgeTuple
        self.routeSet = set()
        for routeIndex, edges in enumerate(allRoutes):
            if self.routePasses(edges):
                self.routeSet.add(routeIndex)
        if self.count > 0 and not self.routeSet:
            print("Warning: no routes pass edge '%s' (count %s)" %
                  (' '.join(self.edgeTuple), self.count), file=sys.stderr)

    def routePasses(self, edges):
        try:
            i = edges.index(self.edgeTuple[0])
            if self.edgeTuple != tuple(edges[i:i + len(self.edgeTuple)]):
                return False
        except ValueError:
            # first edge not in route
            return False
        return True


def parseTurnCounts(fnames, allRoutes, attr):
    result = []
    for fname in fnames:
        for interval in sumolib.xml.parse(fname, 'interval'):
            for fromEdge in interval.fromEdge:
                for toEdge in fromEdge.toEdge:
                    result.append(CountData(int(getattr(toEdge, attr)),
                                            (fromEdge.id, toEdge.id), allRoutes))
    return result


def parseEdgeCounts(fnames, allRoutes, attr):
    result = []
    for fname in fnames:
        for interval in sumolib.xml.parse(fname, 'interval'):
            for edge in interval.edge:
                result.append(CountData(int(getattr(edge, attr)),
                                        (edge.id,), allRoutes))
    return result


def parseTimeRange(fnames):
    begin = 1e20
    end = 0
    for fname in fnames:
        for interval in sumolib.xml.parse(fname, 'interval'):
            begin = min(begin, float(interval.begin))
            end = max(end, float(interval.end))
    return begin, end


def hasCapacity(dataIndices, countData):
    for i in dataIndices:
        if countData[i].count == 0:
            return False
    return True


def updateOpenRoutes(openRoutes, routeUsage, countData):
    return set(filter(lambda r: hasCapacity(routeUsage[r], countData), openRoutes))


def updateOpenCounts(openCounts, countData, openRoutes):
    return set(filter(lambda i: countData[i].routeSet.intersection(openRoutes), openCounts))

def optimize(optimize, countData, routes, usedRoutes, routeUsage):
    """ use relaxtion of the ILP problem for picking the number of times that each route is used
    x = usageCount vector (count for each route index)
    c = weight vector (vector of 1s)
    A_eq = routeUsage encoding
    b_eq = counts

    Rationale:
      c: costs for using each route,
         when minimizing x @ c, routes that pass multiple counting stations are getting an advantage

    """
    import scipy.optimize as opt
    import numpy as np

    k = len(routes)
    m = len(countData)

    if optimize == "full":
        # allow changing all prior usedRoutes
        bounds = None
    else:
        u = int(optimize)
        # limited optimization: change prior routeCounts by at most u per route
        priorRouteCounts = [0] * k
        for r in usedRoutes:
            priorRouteCounts[r] += 1
        bounds = [(max(0, c - u), c + u) for c in priorRouteCounts] + [(0, None)] * m

    # Ax <= b
    # x + s = b
    # min s
    # -> x2 = [x, s]

    c = np.concatenate((np.zeros(k), np.ones(m))) # [x, s], only s counts for minimization
    b = np.asarray([cd.origCount for cd in countData])

    A = np.zeros((m, k))
    for i in range(0, m):
        for j in range(0, k):
            A[i][j] = int(j in countData[i].routeSet)
    A_eq = np.concatenate((A, np.identity(m)), 1)

    res = opt.linprog(c, A_eq=A_eq, b_eq=b, bounds=bounds)
    if res.success:
        print("Optimization succeeded")
        routeCounts = res.x[:k] # cut of slack variables
        slack = res.x[k:]
        #print("routeCounts (n=%s, sum=%s, intSum=%s, roundSum=%s) %s" % (
        #    len(routeCounts),
        #    sum(routeCounts),
        #    sum(map(int, routeCounts)),
        #    sum(map(round, routeCounts)),
        #    routeCounts))
        #print("slack (n=%s, sum=%s) %s" % (len(slack), sum(slack), slack))
        del usedRoutes[:]
        usedRoutes.extend(sum([[i] * int(round(c)) for i, c in enumerate(routeCounts)], []))
        random.shuffle(usedRoutes)
        #print("#usedRoutes=%s" % len(usedRoutes))
        # update countData
        for cd in countData:
            cd.count = cd.origCount
        for r in usedRoutes:
            for i in routeUsage[r]:
                countData[i].count -= 1
    else:
        print("Optimization failed")


def main(options):
    if options.seed:
        random.seed(options.seed)

    # store which routes are passing each counting location (using route index)
    routes = [r.edges.split() for r in sumolib.xml.parse(options.routeFile, 'route')]
    countData = (parseTurnCounts(options.turnFile, routes, options.turnAttr)
                 + parseEdgeCounts(options.edgeDataFile, routes, options.edgeDataAttr))

    # store which counting locations are used by each route (using countData index)
    routeUsage = [set() for r in routes]
    for i, cd in enumerate(countData):
        for routeIndex in cd.routeSet:
            routeUsage[routeIndex].add(i)

    if options.verbose:
        edgeCount = sumolib.miscutils.Statistics("route edge count", histogram=True)
        detectorCount = sumolib.miscutils.Statistics("route detector count", histogram=True)
        for i, edges in enumerate(routes):
            edgeCount.add(len(edges), i)
            detectorCount.add(len(routeUsage[i]), i)
        print("input %s" % edgeCount);
        print("input %s" % detectorCount);

    # pick a random couting location and select a new route that passes it until
    # all counts are satisfied or no routes can be used anymore
    openRoutes = set(range(0, len(routes)))
    openCounts = set(range(0, len(countData)))
    openRoutes = updateOpenRoutes(openRoutes, routeUsage, countData)
    openCounts = updateOpenCounts(openCounts, countData, openRoutes)

    usedRoutes = []
    if options.optimizeInput:
        for routeIndex in range(len(routes)):
            usedRoutes.append(routeIndex)
            for dataIndex in routeUsage[routeIndex]:
                countData[dataIndex].count -= 1
    else:
        while openCounts:
            cd = countData[random.sample(openCounts, 1)[0]]
            routeIndex = random.sample(cd.routeSet.intersection(openRoutes), 1)[0]
            usedRoutes.append(routeIndex)
            for dataIndex in routeUsage[routeIndex]:
                countData[dataIndex].count -= 1
            openRoutes = updateOpenRoutes(openRoutes, routeUsage, countData)
            openCounts = updateOpenCounts(openCounts, countData, openRoutes)

    hasMismatch = sum([cd.count for cd in countData]) > 0
    if hasMismatch and options.optimize is not None:
        optimize(options.optimize, countData, routes, usedRoutes, routeUsage)

    begin, end = parseTimeRange(options.turnFile + options.edgeDataFile)
    with open(options.out, 'w') as outf:
        sumolib.writeXMLHeader(outf, "$Id$", "routes")  # noqa
        period = (end - begin) / len(usedRoutes)
        depart = begin
        for i, routeIndex in enumerate(usedRoutes):
            outf.write('    <vehicle id="%s%s" depart="%s"%s>\n' % (
                options.prefix, i, depart, options.vehattrs))
            outf.write('        <route edges="%s"/>\n' % ' '.join(routes[routeIndex]))
            outf.write('    </vehicle>\n')
            depart += period
        outf.write('</routes>\n')

    underflow = sumolib.miscutils.Statistics("underflow locations")
    overflow = sumolib.miscutils.Statistics("overflow locations")
    totalCount = 0
    for cd in countData:
        totalCount += cd.origCount - cd.count
        if cd.count > 0:
            underflow.add(cd.count, cd.edgeTuple)
        elif cd.count < 0:
            overflow.add(cd.count, cd.edgeTuple)

    print("Wrote %s routes (%s distinct) achieving total count %s at %s locations" % (
        len(usedRoutes), len(set(usedRoutes)), totalCount, len(countData)))

    if options.verbose:
        edgeCount = sumolib.miscutils.Statistics("route edge count", histogram=True)
        detectorCount = sumolib.miscutils.Statistics("route detector count", histogram=True)
        for i, r in enumerate(usedRoutes):
            edgeCount.add(len(routes[r]), i)
            detectorCount.add(len(routeUsage[r]), i)
        print("result %s" % edgeCount);
        print("result %s" % detectorCount);

    if underflow.count() > 0:
        print("Warning: %s (total %s)" % (underflow, sum(underflow.values)))
    if overflow.count() > 0:
        print("Warning: %s (total %s)" % (overflow, sum(overflow.values)))

    if options.deficitOut:
        with open(options.deficitOut, 'w') as outf:
            sumolib.writeXMLHeader(outf, "$Id$")  # noqa
            outf.write('<data>\n')
            outf.write('    <interval id="deficit" begin="0" end="3600">\n')
            for cd in countData:
                if len(cd.edgeTuple) == 1:
                    outf.write('        <edge id="%s" measuredCount="%s" deficit="%s"/>\n' % (
                        cd.edgeTuple[0], cd.origCount, cd.count))
                elif len(cd.edgeTuple) == 2:
                    outf.write('        <edgeRel from="%s" to="%s" measuredCount="%s" deficit="%s"/>\n' % (
                        cd.edgeTuple[0], cd.edgeTuple[1], cd.origCount, cd.count))
                else:
                    print("Warning: output for edge relations with more than 2 edges not supported (%s)" % cd.edgeTuple, file=sys.stderr)
            outf.write('    </interval>\n')
            outf.write('</data>\n')


if __name__ == "__main__":
    main(get_options())