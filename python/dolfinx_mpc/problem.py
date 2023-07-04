# -*- coding: utf-8 -*-
# Copyright (C) 2021 Jørgen S. Dokken
#
# This file is part of DOLFINx MPC
#
# SPDX-License-Identifier:    MIT

import typing

import ufl
from dolfinx import cpp as _cpp
from dolfinx import fem as _fem
from petsc4py import PETSc

from .assemble_matrix import assemble_matrix, create_sparsity_pattern
from .assemble_vector import apply_lifting, assemble_vector
from .multipointconstraint import MultiPointConstraint


class LinearProblem(_fem.petsc.LinearProblem):
    """
    Class for solving a linear variational problem with multi point constraints of the form
    a(u, v) = L(v) for all v using PETSc as a linear algebra backend.

    Args:
        a: A bilinear UFL form, the left hand side of the variational problem.
        L: A linear UFL form, the right hand side of the variational problem.
        mpc: The multi point constraint.
        bcs: A list of Dirichlet boundary conditions.
        u: The solution function. It will be created if not provided. The function has
            to be based on the functionspace in the mpc, i.e.

            .. highlight:: python
            .. code-block:: python

                u = dolfinx.fem.Function(mpc.function_space)
        petsc_options: Parameters that is passed to the linear algebra backend PETSc.
            For available choices for the 'petsc_options' kwarg, see the PETSc-documentation
            https://www.mcs.anl.gov/petsc/documentation/index.html.
        form_compiler_options: Parameters used in FFCx compilation of this form. Run `ffcx --help` at
            the commandline to see all available options. Takes priority over all
            other parameter values, except for `scalar_type` which is determined by DOLFINx.
        jit_options: Parameters used in CFFI JIT compilation of C code generated by FFCx.
            See https://github.com/FEniCS/dolfinx/blob/main/python/dolfinx/jit.py#L22-L37
            for all available parameters. Takes priority over all other parameter values.
    Examples:
        Example usage:

        .. highlight:: python
        .. code-block:: python

           problem = LinearProblem(a, L, mpc, [bc0, bc1],
                                   petsc_options={"ksp_type": "preonly", "pc_type": "lu"})

    """
    u: _fem.Function
    _a: _fem.Form
    _L: _fem.Form
    _mpc: MultiPointConstraint
    _A: PETSc.Mat
    _b: PETSc.Vec
    _solver: PETSc.KSP
    _x: PETSc.Vec
    bcs: typing.List[_fem.DirichletBC]
    __slots__ = tuple(__annotations__)

    def __init__(self, a: ufl.Form, L: ufl.Form, mpc: MultiPointConstraint,
                 bcs: typing.Optional[typing.List[_fem.DirichletBC]] = None,
                 u: typing.Optional[_fem.Function] = None,
                 petsc_options: typing.Optional[dict] = None,
                 form_compiler_options: typing.Optional[dict] = None, jit_options: typing.Optional[dict] = None):

        # Compile forms
        form_compiler_options = {} if form_compiler_options is None else form_compiler_options
        jit_options = {} if jit_options is None else jit_options
        self._a = _fem.form(a, jit_options=jit_options, form_compiler_options=form_compiler_options)
        self._L = _fem.form(L, jit_options=jit_options, form_compiler_options=form_compiler_options)

        if not mpc.finalized:
            raise RuntimeError("The multi point constraint has to be finalized before calling initializer")
        self._mpc = mpc
        # Create function containing solution vector
        if u is None:
            self.u = _fem.Function(self._mpc.function_space)
        else:
            if u.function_space is self._mpc.function_space:
                self.u = u
            else:
                raise ValueError("The input function has to be in the function space in the multi-point constraint",
                                 "i.e. u = dolfinx.fem.Function(mpc.function_space)")
        self._x = self.u.vector

        # Create MPC matrix
        pattern = create_sparsity_pattern(self._a, self._mpc)
        pattern.finalize()
        self._A = _cpp.la.petsc.create_matrix(self._mpc.function_space.mesh.comm, pattern)

        self._b = _cpp.la.petsc.create_vector(self._mpc.function_space.dofmap.index_map,
                                              self._mpc.function_space.dofmap.index_map_bs)
        self.bcs = [] if bcs is None else bcs

        self._solver = PETSc.KSP().create(self.u.function_space.mesh.comm)
        self._solver.setOperators(self._A)

        # Give PETSc solver options a unique prefix
        solver_prefix = "dolfinx_mpc_solve_{}".format(id(self))
        self._solver.setOptionsPrefix(solver_prefix)

        # Set PETSc options
        opts = PETSc.Options()
        opts.prefixPush(solver_prefix)
        if petsc_options is not None:
            for k, v in petsc_options.items():
                opts[k] = v
        opts.prefixPop()
        self._solver.setFromOptions()

    def solve(self) -> _fem.Function:
        """Solve the problem.

        Returns:
            Function containing the solution"""

        # Assemble lhs
        self._A.zeroEntries()
        assemble_matrix(self._a, self._mpc, bcs=self.bcs, A=self._A)
        self._A.assemble()
        assert self._A.assembled

        # Assemble rhs
        with self._b.localForm() as b_loc:
            b_loc.set(0)
        assemble_vector(self._L, self._mpc, b=self._b)

        # Apply boundary conditions to the rhs
        apply_lifting(self._b, [self._a], [self.bcs], self._mpc)
        self._b.ghostUpdate(addv=PETSc.InsertMode.ADD, mode=PETSc.ScatterMode.REVERSE)
        _fem.petsc.set_bc(self._b, self.bcs)

        # Solve linear system and update ghost values in the solution
        self._solver.solve(self._b, self._x)
        self.u.x.scatter_forward()
        self._mpc.backsubstitution(self.u)

        return self.u
