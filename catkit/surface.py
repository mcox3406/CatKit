from . import utils
import numpy as np
from numpy.linalg import norm
from numpy.linalg import solve
from ase.neighborlist import NeighborList
import networkx as nx
import networkx.algorithms.isomorphism as iso
from ase.build import rotate
from ase.constraints import FixAtoms
try:
    from math import gcd
except ImportError:
    from fractions import gcd


class SlabGenerator(object):
    """ Class for generation of slab unit cells from bulk unit cells.
    """

    def __init__(
            self,
            bulk,
            miller_index=[1, 1, 1],
            layers=4,
            fixed=2,
            vacuum=0,
            tol=1e-8):
        """ Generate a slab from an ASE bulk atoms-object.

        Parameters:
          bulk: ASE atoms-object
            Bulk structure to produce the slab from.

          miller_index: list (3,)
            Miller index to construct surface from.

          layers: int
            Number of layers to include in the slab.

          fixed: int
            Number of layers to fix in the slab.

          vacuum: float
            Angstroms of vacuum to add to the slab.

          tol: float
            Tolerance for floating point rounding errors.
        """

        self.bulk = bulk
        self.miller_index = np.array(miller_index)
        self.layers = layers
        self.fixed = fixed
        self.vacuum = vacuum
        self.tol = tol

        self._unique_terminations = None
        self._basis = self.build_basis()

    def build_basis(self):
        """ Get the basis unit cell from bulk unit cell. This
        basis is effectively the same as the bulk, but rotated such
        that the z-axis is aligned with the surface termination.

        The basis is stored separately from the slab generated. This
        is temporary until a slab class is created.

        Returns:
          basis: ASE atoms-object
            The basis slab corresponding to the provided bulk.
        """

        h, k, l = self.miller_index
        h0, k0, l0 = (self.miller_index == 0)
        if h0 and k0 or h0 and l0 or k0 and l0:
            if not h0:
                c1, c2, c3 = [(0, 1, 0), (0, 0, 1), (1, 0, 0)]
            if not k0:
                c1, c2, c3 = [(0, 0, 1), (1, 0, 0), (0, 1, 0)]
            if not l0:
                c1, c2, c3 = [(1, 0, 0), (0, 1, 0), (0, 0, 1)]
        else:
            p, q = ext_gcd(k, l)
            a1, a2, a3 = self.bulk.cell

            # constants describing the dot product of basis c1 and c2:
            # dot(c1,c2) = k1+i*k2, i in Z
            k1 = np.dot(p * (k * a1 - h * a2) + q * (l * a1 - h * a3),
                        l * a2 - k * a3)
            k2 = np.dot(l * (k * a1 - h * a2) - k * (l * a1 - h * a3),
                        l * a2 - k * a3)

            if abs(k2) > self.tol:
                # i corresponding to the optimal basis
                i = -int(round(k1 / k2))
                p, q = p + i * l, q - i * k

            a, b = ext_gcd(p * k + q * l, h)

            c1 = (p * k + q * l, -p * h, -q * h)
            c2 = np.array((0, l, -k)) // abs(gcd(l, k))
            c3 = (b, a * p, a * q)

        slab = self.bulk.copy()
        basis = np.array([c1, c2, c3])

        scaled = solve(basis.T, slab.get_scaled_positions().T).T
        scaled -= np.floor(scaled + self.tol)
        slab.set_scaled_positions(scaled)
        slab.set_cell(np.dot(basis, slab.cell), scale_atoms=True)

        a1, a2, a3 = slab.cell
        a3 = np.cross(a1, a2) / norm(np.cross(a1, a2))
        rotate(slab, a3, (0, 0, 1), a1, (1, 0, 0))

        return slab

    def get_unique_terminations(self):
        """ Return smallest unit cell corresponding to given surface and
        unique surface terminations based on symmetry and nearest neighbors.

        Returns:
          unique_terminations: list
            Unique terminations of a surface.
        """

        # Find all different planes as simply different z-coordinates
        z_planes = utils.get_unique_coordinates(self._basis, tol=self.tol)

        # now get the symmetries of lattice
        symmetry = utils.get_symmetry(self._basis, tol=self.tol)
        rotations = symmetry['rotations']
        translations = symmetry['translations']

        # Find all symmetries which are rotations about the z-axis
        z_symmetry = []
        for i, rotation in enumerate(rotations):
            if (abs(rotation[2][0]) < self.tol and
                abs(rotation[2][1]) < self.tol and
                abs(rotation[0][2]) < self.tol and
                abs(rotation[1][2]) < self.tol and
                abs(rotation[2][2] - 1.0) < self.tol):

                if not np.isclose(
                        translations[i][2],
                        z_symmetry,
                        rtol=self.tol).any():
                    z_symmetry += [translations[i][2]]

        # Find all unique z-shifts
        unique_shift = [z_planes[0]]
        for i in range(1, len(z_planes)):
            symmetry_found = False
            for j in range(0, i):
                z_diff = z_planes[i] - z_planes[j]
                for z_sym in z_symmetry:
                    if np.allclose(z_sym, z_diff, rtol=self.tol):
                        symmetry_found = True
                        break
                else:
                    continue
                break

            if not symmetry_found:
                unique_shift += [z_planes[i]]

        if len(unique_shift) == 1:
            return unique_shift

        # Now search symmetrically unique planes
        # For nearest-neighbor uniqueness
        unique_terminations, graphs = [], []
        for i, z_shift in enumerate(unique_shift):
            tmp_slab = self._basis.copy()
            tmp_slab.translate([0, 0, -z_shift])
            tmp_slab.wrap(pbc=[1, 1, 1])

            zpos = tmp_slab.get_scaled_positions()[:, 2]
            index = np.arange(len(tmp_slab))
            del tmp_slab[index[zpos < 0.5]]

            nl = NeighborList(
                [2] * len(tmp_slab),
                skin=0.0,
                bothways=True,
                self_interaction=False)
            nl.build(tmp_slab)

            G = nx.MultiGraph()
            symbols = tmp_slab.get_chemical_symbols()
            for node, neighbors in enumerate(nl.neighbors):
                G.add_node(node, symbols=symbols[node])
                d = tmp_slab.get_distances(node, neighbors, mic=True)
                edges = [[node, _, {'distance': d[i]}] for i, _ in
                         enumerate(nl.get_neighbors(node)[0])]
                G.add_edges_from(edges)

            isomorph = False
            for G0 in graphs:
                nm = iso.categorical_node_match('symbols', 'X')
                em = iso.numerical_multiedge_match('distance', 1)
                if nx.is_isomorphic(
                        G, G0,
                        edge_match=em,
                        node_match=nm):
                    isomorph = True
                    break

            if not isomorph:
                graphs += [G]
                unique_terminations += [z_shift]

        self._unique_terminations = unique_terminations

        return unique_terminations

    def get_slab(
            self,
            iterm=None,
            primitive=False
    ):
        """Generate a slab object with a certain number of layers.

        Parameters:
          primitive: bool
            Whether to reduce the unit cell to its primitive form.

          iterm: int
            A termination index in reference to the list of possible
            terminations.

        Returns:
          slab: ASE atoms-object
            The modified basis slab produced based on the layer specifications
            given.
        """

        slab = self._basis.copy()

        if iterm:
            if self._unique_terminations is None:
                terminations = self.get_unique_terminations()
            else:
                terminations = self._unique_terminations
            zshift = terminations[iterm]

            slab.translate([0, 0, -zshift])
            slab.wrap(pbc=True)

        # Get the minimum number of layers needed
        zlayers = utils.get_unique_coordinates(
            slab,
            direct=False,
            tol=1e-5
        )
        z_repetitions = np.ceil(self.layers / len(zlayers))
        slab *= (1, 1, int(z_repetitions))

        # Orthogonolize the z-coordinate
        # Warning: bulk symmetry is lost at this point
        a1, a2, a3 = slab.cell
        a3 = (np.cross(a1, a2) * np.dot(a3, np.cross(a1, a2)) /
              norm(np.cross(a1, a2)) ** 2)
        slab.cell[2] = a3

        # Get the direct z-coordinate of the requested layer
        zlayers = utils.get_unique_coordinates(
            slab,
            direct=False,
            tag=True,
            tol=self.tol
        )
        ncut = sorted(zlayers)[::-1][:self.layers][-1]

        zpos = slab.positions[:, 2]
        index = np.arange(len(slab))
        del slab[index[zpos - ncut < -self.tol]]

        slab.cell[2][2] -= ncut
        slab.translate([0, 0, -ncut])

        if self.vacuum:
            slab.center(vacuum=self.vacuum, axis=2)

            if primitive:
                slab = utils.get_primitive_cell(slab)
                zlayers = utils.get_unique_coordinates(
                    slab,
                    direct=False,
                    tag=True,
                    tol=self.tol
                )
                slab.rotate(slab.cell[0], 'x', rotate_cell=True)

        del slab.constraints

        tags = slab.get_tags()
        fix = tags.max() - self.fixed

        constraints = FixAtoms(indices=[a.index for a in slab if a.tag > fix])
        slab.set_constraint(constraints)

        slab.wrap()
        slab.pbc = [1, 1, 0]

        return slab

    def get_surface_atoms(self, slab):

        ind, N = utils.get_voronoi_neighbors(self.bulk)

        radii = [self.bulk.get_distance(u, v, mic=True) for u, v in N.keys()]

        ind0, N0 = utils.get_cutoff_neighbors(slab, cutoff=max(radii))

        ind = np.repeat(ind, np.ceil(len(ind0) / len(ind)))
        surf_atoms = np.nonzero(ind0 - ind[:len(ind0)])[0]

        hwp = slab.positions[surf_atoms] - slab.get_center_of_mass()
        top = surf_atoms[hwp.T[2] > 0]
        bottom = surf_atoms[hwp.T[2] < 0]

        return top, bottom


def ext_gcd(a, b):
    if b == 0:
        return 1, 0
    elif a % b == 0:
        return 0, 1
    else:
        x, y = ext_gcd(b, a % b)
        return y, x - y * (a // b)
