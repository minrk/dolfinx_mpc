# -*- coding: utf-8 -*-
# Copyright (C) 2021 Jørgen S. Dokken
#
# This file is part of DOLFINx MPC
#
# SPDX-License-Identifier:    LGPL-3.0-or-later

import typing
import ufl
from dolfinx import fem
from dolfinx import cpp
import dolfinx_mpc.cpp
from petsc4py import PETSc
from .assemble_matrix import assemble_matrix
from .assemble_vector import assemble_vector
from .multipointconstraint import MultiPointConstraint


class LinearProblem():
    """Class for solving a linear variational problem with multi point constraints of the form
    a(u, v) = L(v) for all v using PETSc as a linear algebra backend.

    """

    def __init__(self, a: ufl.Form, L: ufl.Form, mpc: MultiPointConstraint, bcs: typing.List[fem.DirichletBC] = [],
                 petsc_options={}, form_compiler_parameters={}, jit_parameters={}):
        """Initialize solver for a linear variational problem.

        Parameters
        ----------
        a
            A bilinear UFL form, the left hand side of the variational problem.

        L
            A linear UFL form, the right hand side of the variational problem.
        mpc
            The multi point constraint.

        bcs
            A list of Dirichlet boundary conditions.

        u
            The solution function. It will be created if not provided.

        petsc_options
            Parameters that is passed to the linear algebra backend PETSc.
            For available choices for the 'petsc_options' kwarg, see the
            `PETSc-documentation <https://www.mcs.anl.gov/petsc/documentation/index.html>`.

        form_compiler_parameters
            Parameters used in FFCx compilation of this form. Run `ffcx --help` at
            the commandline to see all available options. Takes priority over all
            other parameter values, except for `scalar_type` which is determined by
            DOLFINx.

        jit_parameters
            Parameters used in CFFI JIT compilation of C code generated by FFCx.
            See `python/dolfinx/jit.py` for all available parameters.
            Takes priority over all other parameter values.

        .. code-block:: python
            problem = LinearProblem(a, L, mpc, [bc0, bc1], petsc_options={"ksp_type": "preonly", "pc_type": "lu"})
        """
        # Store jit and form parameters for matrix and vector assembly
        self._a = a
        self._L = L
        self._form_compiler_parameters = form_compiler_parameters
        self._jit_parameters = jit_parameters

        if not mpc.finalized:
            raise RuntimeError("The multi point constraint has to be finalized before calling initializer")
        self._mpc = mpc
        # Create function containing solution vector
        self.u = fem.Function(self._mpc.function_space())

        # NOTE: This is a workaround for only creating sparsity pattern once
        a_cpp = fem.Form(a, form_compiler_parameters=form_compiler_parameters,
                         jit_parameters=jit_parameters)._cpp_object

        # Create MPC matrix
        pattern = dolfinx_mpc.cpp.mpc.create_sparsity_pattern(a_cpp, self._mpc._cpp_object)
        pattern.assemble()
        self._A = cpp.la.create_matrix(self._mpc.function_space().mesh.mpi_comm(), pattern)

        self._b = cpp.la.create_vector(self._mpc.index_map(), self._mpc.function_space().dofmap.index_map_bs)
        self.bcs = bcs

        self._solver = PETSc.KSP().create(self.u.function_space.mesh.mpi_comm())
        self._solver.setOperators(self._A)

        # Give PETSc solver options a unique prefix
        solver_prefix = "dolfinx_mpc_solve_{}".format(id(self))
        self._solver.setOptionsPrefix(solver_prefix)

        # Set PETSc options
        opts = PETSc.Options()
        opts.prefixPush(solver_prefix)
        for k, v in petsc_options.items():
            opts[k] = v
        opts.prefixPop()
        self._solver.setFromOptions()

    def solve(self) -> fem.Function:
        """Solve the problem. Return a dolfinx function containing the solution"""

        # Assemble lhs
        self._A.zeroEntries()
        assemble_matrix(self._a, self._mpc, bcs=self.bcs, A=self._A,
                        form_compiler_parameters=self._form_compiler_parameters, jit_parameters=self._jit_parameters)
        self._A.assemble()

        # Assemble rhs
        with self._b.localForm() as b_loc:
            b_loc.set(0)
        assemble_vector(self._L, self._mpc, b=self._b, form_compiler_parameters=self._form_compiler_parameters,
                        jit_parameters=self._jit_parameters)

        # Apply boundary conditions to the rhs
        fem.apply_lifting(self._b, [self._a], [self.bcs])
        self._b.ghostUpdate(addv=PETSc.InsertMode.ADD, mode=PETSc.ScatterMode.REVERSE)
        fem.set_bc(self._b, self.bcs)

        # Solve linear system and update ghost values in the solution
        self._solver.solve(self._b, self.u.vector)
        self.u.x.scatter_forward()
        self._mpc.backsubstitution(self.u.vector)

        return self.u

    # FIXME: Add these using new interface for assemble_matrix and assemble_vector
    # @property
    # def L(self) -> fem.Form:
    #     """Get the compiled linear form"""
    #     return self._L

    # @property
    # def a(self) -> fem.Form:
    #     """Get the compiled bilinear form"""
    #     return self._a

    @property
    def A(self) -> PETSc.Mat:
        """Get the matrix operator"""
        return self._A

    @property
    def b(self) -> PETSc.Vec:
        """Get the RHS vector"""
        return self._b

    @property
    def solver(self) -> PETSc.KSP:
        """Get the linear solver"""
        return self._solver
