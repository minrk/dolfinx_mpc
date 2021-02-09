// Copyright (C) 2020 Jørgen S. Dokken
//
// This file is part of DOLFINX-MPC
//
// SPDX-License-Identifier:    LGPL-3.0-or-later

#include "array.h"
#include "caster_petsc.h"
#include <Eigen/Dense>
#include <dolfinx/common/IndexMap.h>
#include <dolfinx/fem/DirichletBC.h>
#include <dolfinx/fem/Form.h>
#include <dolfinx/fem/FunctionSpace.h>
#include <dolfinx/geometry/BoundingBoxTree.h>
#include <dolfinx/geometry/utils.h>
#include <dolfinx/la/PETScMatrix.h>
#include <dolfinx_mpc/ContactConstraint.h>
#include <dolfinx_mpc/MultiPointConstraint.h>
#include <dolfinx_mpc/assembly.h>
#include <dolfinx_mpc/utils.h>
#include <memory>
#include <petscmat.h>
#include <petscvec.h>
#include <pybind11/eigen.h>
#include <pybind11/functional.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
namespace py = pybind11;

namespace dolfinx_mpc_wrappers
{
void mpc(py::module& m)
{

  m.def("get_basis_functions", &dolfinx_mpc::get_basis_functions);
  m.def("compute_shared_indices", &dolfinx_mpc::compute_shared_indices);
  m.def("add_pattern_diagonal",
        [](dolfinx::la::SparsityPattern& pattern,
           const py::array_t<std::int32_t, py::array::c_style>& blocks) {
          dolfinx_mpc::add_pattern_diagonal(
              pattern, tcb::span(blocks.data(), blocks.size()));
        });

  // dolfinx_mpc::MultiPointConstraint
  py::class_<dolfinx_mpc::MultiPointConstraint,
             std::shared_ptr<dolfinx_mpc::MultiPointConstraint>>
      multipointconstraint(
          m, "MultiPointConstraint",
          "Object for representing contact (non-penetrating) conditions");
  multipointconstraint
      .def(py::init<std::shared_ptr<const dolfinx::fem::FunctionSpace>,
                    std::vector<std::int32_t>, std::int32_t>())
      .def("slaves",
           [](dolfinx_mpc::MultiPointConstraint& self) {
             const std::vector<std::int32_t>& slaves = self.slaves();
             return py::array_t<std::int32_t>(slaves.size(), slaves.data(),
                                              py::cast(self));
           })
      .def("slave_cells",
           [](dolfinx_mpc::MultiPointConstraint& self) {
             const std::vector<std::int32_t>& slave_cells = self.slave_cells();
             return py::array_t<std::int32_t>(
                 slave_cells.size(), slave_cells.data(), py::cast(self));
           })
      .def("slave_to_cells", &dolfinx_mpc::MultiPointConstraint::slave_to_cells)
      .def("add_masters", &dolfinx_mpc::MultiPointConstraint::add_masters)
      .def("cell_to_slaves", &dolfinx_mpc::MultiPointConstraint::cell_to_slaves)
      .def("masters_local", &dolfinx_mpc::MultiPointConstraint::masters_local)
      .def("coefficients",
           [](dolfinx_mpc::MultiPointConstraint& self) {
             const std::vector<PetscScalar>& coefficients = self.coefficients();
             return py::array_t<PetscScalar>(
                 coefficients.size(), coefficients.data(), py::cast(self));
           })
      .def("create_sparsity_pattern",
           &dolfinx_mpc::MultiPointConstraint::create_sparsity_pattern)
      .def_property_readonly(
          "num_local_slaves",
          &dolfinx_mpc::MultiPointConstraint::num_local_slaves)
      .def("index_map", &dolfinx_mpc::MultiPointConstraint::index_map)
      .def("dofmap", &dolfinx_mpc::MultiPointConstraint::dofmap)
      .def("owners", &dolfinx_mpc::MultiPointConstraint::owners)
      .def(
          "backsubstitution",
          [](dolfinx_mpc::MultiPointConstraint& self,
             py::array_t<PetscScalar, py::array::c_style> u) {
            self.backsubstitution(tcb::span(u.mutable_data(), u.size()));
          },
          py::arg("u"), "Backsubstitute slave values into vector");

  py::class_<dolfinx_mpc::mpc_data, std::shared_ptr<dolfinx_mpc::mpc_data>>
      mpc_data(m, "mpc_data", "Object with data arrays for mpc");
  mpc_data.def("get_slaves", &dolfinx_mpc::mpc_data::get_slaves)
      .def("get_masters", &dolfinx_mpc::mpc_data::get_masters)
      .def("get_coeffs", &dolfinx_mpc::mpc_data::get_coeffs)
      .def("get_owners", &dolfinx_mpc::mpc_data::get_owners)
      .def("get_offsets", &dolfinx_mpc::mpc_data::get_offsets);

  //   .def("ghost_masters", &dolfinx_mpc::mpc_data::ghost_masters);

  m.def("assemble_matrix",
        [](Mat A, const dolfinx::fem::Form<PetscScalar>& a,
           const std::shared_ptr<const dolfinx_mpc::MultiPointConstraint>& mpc,
           const std::vector<std::shared_ptr<
               const dolfinx::fem::DirichletBC<PetscScalar>>>& bcs) {
          dolfinx_mpc::assemble_matrix(
              dolfinx::la::PETScMatrix::add_block_fn(A),
              dolfinx::la::PETScMatrix::add_fn(A), a, mpc, bcs);
        });

  m.def(
      "create_matrix",
      [](const dolfinx::fem::Form<PetscScalar>& a,
         const std::shared_ptr<dolfinx_mpc::MultiPointConstraint>& mpc) {
        auto A = dolfinx_mpc::create_matrix(a, mpc);
        Mat _A = A.mat();
        PetscObjectReference((PetscObject)_A);
        return _A;
      },
      py::return_value_policy::take_ownership,
      "Create a PETSc Mat for bilinear form.");
  m.def("create_contact_slip_condition",
        &dolfinx_mpc::create_contact_slip_condition);
  m.def("create_contact_inelastic_condition",
        &dolfinx_mpc::create_contact_inelastic_condition);
  m.def("create_dof_to_facet_map", &dolfinx_mpc::create_dof_to_facet_map);
  m.def("create_average_normal", &dolfinx_mpc::create_average_normal);
  m.def("create_normal_approximation",
        [](std::shared_ptr<dolfinx::fem::FunctionSpace> V,
           const py::array_t<std::int32_t, py::array::c_style>& entities,
           py::array_t<PetscScalar, py::array::c_style> vector) {
          return dolfinx_mpc::create_normal_approximation(
              V, tcb::span(entities.data(), entities.size()),
              tcb::span(vector.mutable_data(), vector.size()));
        });
}
} // namespace dolfinx_mpc_wrappers
