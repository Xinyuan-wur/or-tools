# Copyright 2010-2018 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Propose a natural language on top of cp_model_pb2 python proto.

This file implements a easy-to-use API on top of the cp_model_pb2 protobuf
defined in ../ .
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import numbers
import time
from six import iteritems

from ortools.sat import cp_model_pb2
from ortools.sat import sat_parameters_pb2
from ortools.sat.python import cp_model_helper
from ortools.sat import pywrapsat

# The classes below allow linear expressions to be expressed naturally with the
# usual arithmetic operators +-*/ and with constant numbers, which makes the
# python API very intuitive. See ../samples/*.py for examples.

INT_MIN = -9223372036854775808  # hardcoded to be platform independent.
INT_MAX = 9223372036854775807
INT32_MAX = 2147483647
INT32_MIN = -2147483648

# Cp Solver status (exported to avoid importing cp_model_cp2).
UNKNOWN = cp_model_pb2.UNKNOWN
MODEL_INVALID = cp_model_pb2.MODEL_INVALID
FEASIBLE = cp_model_pb2.FEASIBLE
INFEASIBLE = cp_model_pb2.INFEASIBLE
OPTIMAL = cp_model_pb2.OPTIMAL

# Variable selection strategy
CHOOSE_FIRST = cp_model_pb2.DecisionStrategyProto.CHOOSE_FIRST
CHOOSE_LOWEST_MIN = cp_model_pb2.DecisionStrategyProto.CHOOSE_LOWEST_MIN
CHOOSE_HIGHEST_MAX = cp_model_pb2.DecisionStrategyProto.CHOOSE_HIGHEST_MAX
CHOOSE_MIN_DOMAIN_SIZE = (
    cp_model_pb2.DecisionStrategyProto.CHOOSE_MIN_DOMAIN_SIZE)
CHOOSE_MAX_DOMAIN_SIZE = (
    cp_model_pb2.DecisionStrategyProto.CHOOSE_MAX_DOMAIN_SIZE)

# Domain reduction strategy
SELECT_MIN_VALUE = cp_model_pb2.DecisionStrategyProto.SELECT_MIN_VALUE
SELECT_MAX_VALUE = cp_model_pb2.DecisionStrategyProto.SELECT_MAX_VALUE
SELECT_LOWER_HALF = cp_model_pb2.DecisionStrategyProto.SELECT_LOWER_HALF
SELECT_UPPER_HALF = cp_model_pb2.DecisionStrategyProto.SELECT_UPPER_HALF

# Search branching
AUTOMATIC_SEARCH = sat_parameters_pb2.SatParameters.AUTOMATIC_SEARCH
FIXED_SEARCH = sat_parameters_pb2.SatParameters.FIXED_SEARCH
PORTFOLIO_SEARCH = sat_parameters_pb2.SatParameters.PORTFOLIO_SEARCH
LP_SEARCH = sat_parameters_pb2.SatParameters.LP_SEARCH


def DisplayBounds(bounds):
    """Displays a flattened list of intervals."""
    out = ''
    for i in range(0, len(bounds), 2):
        if i != 0:
            out += ', '
        if bounds[i] == bounds[i + 1]:
            out += str(bounds[i])
        else:
            out += str(bounds[i]) + '..' + str(bounds[i + 1])
    return out


def ShortName(model, i):
    """Returns a short name of an integer variable, or its negation."""
    if i < 0:
        return 'Not(%s)' % ShortName(model, -i - 1)
    v = model.variables[i]
    if v.name:
        return v.name
    elif len(v.domain) == 2 and v.domain[0] == v.domain[1]:
        return str(v.domain[0])
    else:
        return '[%s]' % DisplayBounds(v.domain)


class LinearExpression(object):
    """Holds an integer linear expression.

  An linear expression is built from integer constants and variables.

  x + 2 * (y - z + 1) is one such linear expression, and can be written that
  way directly in Python, provided x, y, and z are integer variables.

  Linear expressions are used in two places in the cp_model.
  When used with equality and inequality operators, they create linear
  inequalities that can be added to the model as in:

      model.Add(x + 2 * y <= 5)
      model.Add(sum(array_of_vars) == 5)

  Linear expressions can also be used to specify the objective of the model.

      model.Minimize(x + 2 * y + z)
  """

    def GetVarValueMap(self):
        """Scan the expression, and return a list of (var_coef_map, constant)."""
        coeffs = collections.defaultdict(int)
        constant = 0
        to_process = [(self, 1)]
        while to_process:  # Flatten to avoid recursion.
            expr, coef = to_process.pop()
            if isinstance(expr, _ProductCst):
                to_process.append((expr.Expression(),
                                   coef * expr.Coefficient()))
            elif isinstance(expr, _SumArray):
                for e in expr.Array():
                    to_process.append((e, coef))
                constant += expr.Constant() * coef
            elif isinstance(expr, IntVar):
                coeffs[expr] += coef
            elif isinstance(expr, _NotBooleanVariable):
                raise TypeError(
                    'Cannot interpret literals in a linear expression.')
            else:
                raise TypeError('Unrecognized linear expression: ' + str(expr))

        return coeffs, constant

    def __hash__(self):
        return object.__hash__(self)

    def __add__(self, expr):
        return _SumArray([self, expr])

    def __radd__(self, arg):
        return _SumArray([self, arg])

    def __sub__(self, expr):
        return _SumArray([self, -expr])

    def __rsub__(self, arg):
        return _SumArray([-self, arg])

    def __mul__(self, arg):
        if isinstance(arg, numbers.Integral):
            if arg == 1:
                return self
            cp_model_helper.AssertIsInt64(arg)
            return _ProductCst(self, arg)
        else:
            raise TypeError('Not an integer linear expression: ' + str(arg))

    def __rmul__(self, arg):
        cp_model_helper.AssertIsInt64(arg)
        if arg == 1:
            return self
        return _ProductCst(self, arg)

    def __div__(self, _):
        raise NotImplementedError('LinearExpression.__div__')

    def __truediv__(self, _):
        raise NotImplementedError('LinearExpression.__truediv__')

    def __mod__(self, _):
        raise NotImplementedError('LinearExpression.__mod__')

    def __neg__(self):
        return _ProductCst(self, -1)

    def __eq__(self, arg):
        if arg is None:
            return False
        if isinstance(arg, numbers.Integral):
            cp_model_helper.AssertIsInt64(arg)
            return LinearInequality(self, [arg, arg])
        else:
            return LinearInequality(self - arg, [0, 0])

    def __ge__(self, arg):
        if isinstance(arg, numbers.Integral):
            cp_model_helper.AssertIsInt64(arg)
            return LinearInequality(self, [arg, INT_MAX])
        else:
            return LinearInequality(self - arg, [0, INT_MAX])

    def __le__(self, arg):
        if isinstance(arg, numbers.Integral):
            cp_model_helper.AssertIsInt64(arg)
            return LinearInequality(self, [INT_MIN, arg])
        else:
            return LinearInequality(self - arg, [INT_MIN, 0])

    def __lt__(self, arg):
        if isinstance(arg, numbers.Integral):
            cp_model_helper.AssertIsInt64(arg)
            if arg == INT_MIN:
                raise ArithmeticError('< INT_MIN is not supported')
            return LinearInequality(
                self, [INT_MIN, cp_model_helper.CapInt64(arg - 1)])
        else:
            return LinearInequality(self - arg, [INT_MIN, -1])

    def __gt__(self, arg):
        if isinstance(arg, numbers.Integral):
            cp_model_helper.AssertIsInt64(arg)
            if arg == INT_MAX:
                raise ArithmeticError('> INT_MAX is not supported')
            return LinearInequality(
                self, [cp_model_helper.CapInt64(arg + 1), INT_MAX])
        else:
            return LinearInequality(self - arg, [1, INT_MAX])

    def __ne__(self, arg):
        if arg is None:
            return True
        if isinstance(arg, numbers.Integral):
            cp_model_helper.AssertIsInt64(arg)
            if arg == INT_MAX:
                return LinearInequality(self, [INT_MIN, INT_MAX - 1])
            elif arg == INT_MIN:
                return LinearInequality(self, [INT_MIN + 1, INT_MAX])
            else:
                return LinearInequality(self, [
                    INT_MIN,
                    cp_model_helper.CapInt64(arg - 1),
                    cp_model_helper.CapInt64(arg + 1), INT_MAX
                ])
        else:
            return LinearInequality(self - arg, [INT_MIN, -1, 1, INT_MAX])


class _ProductCst(LinearExpression):
    """Represents the product of a LinearExpression by a constant."""

    def __init__(self, expr, coef):
        cp_model_helper.AssertIsInt64(coef)
        if isinstance(expr, _ProductCst):
            self.__expr = expr.Expression()
            self.__coef = expr.Coefficient() * coef
        else:
            self.__expr = expr
            self.__coef = coef

    def __str__(self):
        if self.__coef == -1:
            return '-' + str(self.__expr)
        else:
            return '(' + str(self.__coef) + ' * ' + str(self.__expr) + ')'

    def __repr__(self):
        return 'ProductCst(' + repr(self.__expr) + ', ' + repr(
            self.__coef) + ')'

    def Coefficient(self):
        return self.__coef

    def Expression(self):
        return self.__expr


class _SumArray(LinearExpression):
    """Represents the sum of a list of LinearExpression and a constant."""

    def __init__(self, array):
        self.__array = []
        self.__constant = 0
        for x in array:
            if isinstance(x, numbers.Integral):
                cp_model_helper.AssertIsInt64(x)
                self.__constant += x
            elif isinstance(x, LinearExpression):
                self.__array.append(x)
            else:
                raise TypeError('Not an linear expression: ' + str(x))

    def __str__(self):
        if self.__constant == 0:
            return '({})'.format(' + '.join(map(str, self.__array)))
        else:
            return '({} + {})'.format(' + '.join(map(str, self.__array)),
                                      self.__constant)

    def __repr__(self):
        return 'SumArray({}, {})'.format(', '.join(map(repr, self.__array)),
                                         self.__constant)

    def Array(self):
        return self.__array

    def Constant(self):
        return self.__constant


class IntVar(LinearExpression):
    """An integer variable.

  An IntVar is an object that can take on any integer value within defined
  ranges. Variables appears in constraint like:

      x + y >= 5
      AllDifferent([x, y, z])

  Solving a model is equivalent to finding, for each variable, a single value
  from the set of initial values (called the initial domain), such that the
  model is feasible, or optimal if you provided an objective function.
  """

    def __init__(self, model, bounds, name):
        """See CpModel.NewIntVar below."""
        self.__model = model
        self.__index = len(model.variables)
        self.__var = model.variables.add()
        self.__var.domain.extend(bounds)
        self.__var.name = name
        self.__negation = None

    def Index(self):
        return self.__index

    def __str__(self):
        return self.__var.name

    def __repr__(self):
        return '%s(%s)' % (self.__var.name, DisplayBounds(self.__var.domain))

    def Name(self):
        return self.__var.name

    def Not(self):
        """Returns the negation of a Boolean variable.

    This method implements the logical negation of a Boolean variable.
    It is only valid of the variable has a Boolean domain (0 or 1).

    Note that this method is nilpotent: x.Not().Not() == x.
    """

        for bound in self.__var.domain:
            if bound < 0 or bound > 1:
                raise TypeError(
                    'Cannot call Not on a non boolean variable: %s' % self)
        if not self.__negation:
            self.__negation = _NotBooleanVariable(self)
        return self.__negation


class _NotBooleanVariable(LinearExpression):
    """Negation of a boolean variable."""

    def __init__(self, boolvar):
        self.__boolvar = boolvar

    def Index(self):
        return -self.__boolvar.Index() - 1

    def Not(self):
        return self.__boolvar

    def __str__(self):
        return 'not(%s)' % str(self.__boolvar)


class LinearInequality(object):
    """Represents a linear constraint: lb <= expression <= ub.

  The only use of this class is to be added to the CpModel through
  CpModel.Add(expression), as in:

      model.Add(x + 2 * y -1 >= z)
  """

    def __init__(self, expr, bounds):
        self.__expr = expr
        self.__bounds = bounds

    def __str__(self):
        if len(self.__bounds) == 2:
            lb = self.__bounds[0]
            ub = self.__bounds[1]
            if lb > INT_MIN and ub < INT_MAX:
                if lb == ub:
                    return str(self.__expr) + ' == ' + str(lb)
                else:
                    return str(lb) + ' <= ' + str(
                        self.__expr) + ' <= ' + str(ub)
            elif lb > INT_MIN:
                return str(self.__expr) + ' >= ' + str(lb)
            elif ub < INT_MAX:
                return str(self.__expr) + ' <= ' + str(ub)
            else:
                return 'True (unbounded expr ' + str(self.__expr) + ')'
        else:
            return str(self.__expr) + ' in [' + DisplayBounds(
                self.__bounds) + ']'

    def Expression(self):
        return self.__expr

    def Bounds(self):
        return self.__bounds


class Constraint(object):
    """Base class for constraints.

  Constraints are built by the CpModel through the Add<XXX> methods.
  Once created by the CpModel class, they are automatically added to the model.
  The purpose of this class is to allow specification of enforcement literals
  for this constraint.

      b = model.BoolVar('b')
      x = model.IntVar(0, 10, 'x')
      y = model.IntVar(0, 10, 'y')

      model.Add(x + 2 * y == 5).OnlyEnforceIf(b.Not())
  """

    def __init__(self, constraints):
        self.__index = len(constraints)
        self.__constraint = constraints.add()

    def OnlyEnforceIf(self, boolvar):
        """Adds an enforcement literal to the constraint.

    Args:
        boolvar: A boolean literal or a list of boolean literals.

    Returns:
        self.

    This method adds one or more literals (that is a boolean variable or its
    negation) as enforcement literals. The conjunction of all these literals
    decides whether the constraint is active or not. It acts as an
    implication, so if the conjunction is true, it implies that the constraint
    must be enforced. If it is false, then the constraint is ignored.

    The following constraints support enforcement literals:
       bool or, bool and, and any linear constraints support any number of
       enforcement literals.
    """

        if isinstance(boolvar, numbers.Integral) and boolvar == 1:
            # Always true. Do nothing.
            pass
        elif isinstance(boolvar, list):
            for b in boolvar:
                if isinstance(b, numbers.Integral) and b == 1:
                    pass
                else:
                    self.__constraint.enforcement_literal.append(b.Index())
        else:
            self.__constraint.enforcement_literal.append(boolvar.Index())
        return self

    def Index(self):
        return self.__index

    def ConstraintProto(self):
        return self.__constraint


class IntervalVar(object):
    """Represents a Interval variable.

  An interval variable is both a constraint and a variable. It is defined by
  three integer variables: start, size, and end.

  It is a constraint because, internally, it enforces that start + size == end.

  It is also a variable as it can appear in specific scheduling constraints:
  NoOverlap, NoOverlap2D, Cumulative.

  Optionally, an enforcement literal can be added to this
  constraint. This enforcement literal is understood by the same constraints.
  These constraints ignore interval variables with enforcement literals assigned
  to false. Conversely, these constraints will also set these enforcement
  literals to false if they cannot fit these intervals into the schedule.
  """

    def __init__(self, model, start_index, size_index, end_index,
                 is_present_index, name):
        self.__model = model
        self.__index = len(model.constraints)
        self.__ct = self.__model.constraints.add()
        self.__ct.interval.start = start_index
        self.__ct.interval.size = size_index
        self.__ct.interval.end = end_index
        if is_present_index is not None:
            self.__ct.enforcement_literal.append(is_present_index)
        if name:
            self.__ct.name = name

    def Index(self):
        return self.__index

    def __str__(self):
        return self.__ct.name

    def __repr__(self):
        interval = self.__ct.interval
        if self.__ct.enforcement_literal:
            return '%s(start = %s, size = %s, end = %s, is_present = %s)' % (
                self.__ct.name, ShortName(self.__model, interval.start),
                ShortName(self.__model, interval.size),
                ShortName(self.__model, interval.end),
                ShortName(self.__model, self.__ct.enforcement_literal[0]))
        else:
            return '%s(start = %s, size = %s, end = %s)' % (
                self.__ct.name, ShortName(self.__model, interval.start),
                ShortName(self.__model, interval.size),
                ShortName(self.__model, interval.end))

    def Name(self):
        return self.__ct.name


class CpModel(object):
    """Wrapper class around the cp_model proto.

  This class provides two types of methods:
    - NewXXX to create integer, boolean, or interval variables.
    - AddXXX to create new constraints and add them to the model.
  """

    def __init__(self):
        self.__model = cp_model_pb2.CpModelProto()
        self.__constant_map = {}
        self.__optional_constant_map = {}

    # Integer variable.

    def NewIntVar(self, lb, ub, name):
        """Creates an integer variable with domain [lb, ub]."""
        return IntVar(self.__model, [lb, ub], name)

    def NewEnumeratedIntVar(self, bounds, name):
        """Creates an integer variable with an enumerated domain.

    Args:
        bounds: A flattened list of disjoint intervals.
        name: The name of the variable.

    Returns:
        a variable whose domain is union[bounds[2*i]..bounds[2*i + 1]].

    To create a variable with domain [1, 2, 3, 5, 7, 8], pass in the
    array [1, 3, 5, 5, 7, 8].
    """
        return IntVar(self.__model, bounds, name)

    def NewBoolVar(self, name):
        """Creates a 0-1 variable with the given name."""
        return IntVar(self.__model, [0, 1], name)

    # Integer constraints.

    def AddLinearConstraint(self, terms, lb, ub):
        """Adds the constraints lb <= sum(terms) <= ub, where term = (var, coef)."""
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        for t in terms:
            if not isinstance(t[0], IntVar):
                raise TypeError('Wrong argument' + str(t))
            cp_model_helper.AssertIsInt64(t[1])
            model_ct.linear.vars.append(t[0].Index())
            model_ct.linear.coeffs.append(t[1])
        model_ct.linear.domain.extend([lb, ub])
        return ct

    def AddSumConstraint(self, variables, lb, ub):
        """Adds the constraints lb <= sum(variables) <= ub."""
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        for v in variables:
            model_ct.linear.vars.append(v.Index())
            model_ct.linear.coeffs.append(1)
        model_ct.linear.domain.extend([lb, ub])
        return ct

    def AddLinearConstraintWithBounds(self, terms, bounds):
        """Adds the constraints sum(terms) in bounds, where term = (var, coef)."""
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        for t in terms:
            if not isinstance(t[0], IntVar):
                raise TypeError('Wrong argument' + str(t))
            cp_model_helper.AssertIsInt64(t[1])
            model_ct.linear.vars.append(t[0].Index())
            model_ct.linear.coeffs.append(t[1])
        model_ct.linear.domain.extend(bounds)
        return ct

    def Add(self, ct):
        """Adds a LinearInequality to the model."""
        if isinstance(ct, LinearInequality):
            coeffs_map, constant = ct.Expression().GetVarValueMap()
            bounds = [cp_model_helper.CapSub(x, constant) for x in ct.Bounds()]
            return self.AddLinearConstraintWithBounds(
                iteritems(coeffs_map), bounds)
        elif ct and isinstance(ct, bool):
            pass  # Nothing to do, was already evaluated to true.
        elif not ct and isinstance(ct, bool):
            return self.AddBoolOr([])  # Evaluate to false.
        else:
            raise TypeError('Not supported: CpModel.Add(' + str(ct) + ')')

    def AddAllDifferent(self, variables):
        """Adds AllDifferent(variables).

    This constraint forces all variables to have different values.

    Args:
      variables: a list of integer variables.

    Returns:
      An instance of the Constraint class.
    """
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.all_diff.vars.extend(
            [self.GetOrMakeIndex(x) for x in variables])
        return ct

    def AddElement(self, index, variables, target):
        """Adds the element constraint: variables[index] == target."""

        if not variables:
            raise ValueError('AddElement expects a non empty variables array')

        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.element.index = self.GetOrMakeIndex(index)
        model_ct.element.vars.extend(
            [self.GetOrMakeIndex(x) for x in variables])
        model_ct.element.target = self.GetOrMakeIndex(target)
        return ct

    def AddCircuit(self, arcs):
        """Adds Circuit(arcs).

    Adds a circuit constraint from a sparse list of arcs that encode the graph.

    A circuit is a unique Hamiltonian path in a subgraph of the total
    graph. In case a node 'i' is not in the path, then there must be a
    loop arc 'i -> i' associated with a true literal. Otherwise
    this constraint will fail.

    Args:
      arcs: a list of arcs. An arc is a tuple (source_node, destination_node,
        literal). The arc is selected in the circuit if the literal is true.
        Both source_node and destination_node must be integer value between 0
        and the number of nodes - 1.

    Returns:
      An instance of the Constraint class.

    Raises:
      ValueError: If the list of arc is empty.
    """
        if not arcs:
            raise ValueError('AddCircuit expects a non empty array of arcs')
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        for arc in arcs:
            cp_model_helper.AssertIsInt32(arc[0])
            cp_model_helper.AssertIsInt32(arc[1])
            lit = self.GetOrMakeBooleanIndex(arc[2])
            model_ct.circuit.tails.append(arc[0])
            model_ct.circuit.heads.append(arc[1])
            model_ct.circuit.literals.append(lit)
        return ct

    def AddAllowedAssignments(self, variables, tuples_list):
        """Adds AllowedAssignments(variables, tuples_list).

    An AllowedAssignments constraint is a constraint on an array of variables
    that forces, when all variables are fixed to a single value, that the
    corresponding list of values is equal to one of the tuple of the
    tuple_list.

    Args:
      variables: A list of variables.
      tuples_list: A list of admissible tuples. Each tuple must have the same
        length as the variables, and the ith value of a tuple corresponds to the
        ith variable.

    Returns:
      An instance of the Constraint class.

    Raises:
      TypeError: If a tuple does not have the same size as the list of
          variables.
      ValueError: If the array of variables is empty.
    """

        if not variables:
            raise ValueError(
                'AddAllowedAssignments expects a non empty variables '
                'array')

        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.table.vars.extend([self.GetOrMakeIndex(x) for x in variables])
        arity = len(variables)
        for t in tuples_list:
            if len(t) != arity:
                raise TypeError('Tuple ' + str(t) + ' has the wrong arity')
            for v in t:
                cp_model_helper.AssertIsInt64(v)
            model_ct.table.values.extend(t)
        return ct

    def AddForbiddenAssignments(self, variables, tuples_list):
        """Adds AddForbiddenAssignments(variables, [tuples_list]).

    A ForbiddenAssignments constraint is a constraint on an array of variables
    where the list of impossible combinations is provided in the tuples list.

    Args:
      variables: A list of variables.
      tuples_list: A list of forbidden tuples. Each tuple must have the same
        length as the variables, and the ith value of a tuple corresponds to the
        ith variable.

    Returns:
      An instance of the Constraint class.

    Raises:
      TypeError: If a tuple does not have the same size as the list of
                 variables.
      ValueError: If the array of variables is empty.
    """

        if not variables:
            raise ValueError(
                'AddForbiddenAssignments expects a non empty variables '
                'array')

        index = len(self.__model.constraints)
        ct = self.AddAllowedAssignments(variables, tuples_list)
        self.__model.constraints[index].table.negated = True
        return ct

    def AddAutomaton(self, transition_variables, starting_state, final_states,
                     transition_triples):
        """Adds an automaton constraint.

    An automaton constraint takes a list of variables (of size n), an initial
    state, a set of final states, and a set of transitions. A transition is a
    triplet ('tail', 'transition', 'head'), where 'tail' and 'head' are states,
    and 'transition' is the label of an arc from 'head' to 'tail',
    corresponding to the value of one variable in the list of variables.

    This automata will be unrolled into a flow with n + 1 phases. Each phase
    contains the possible states of the automaton. The first state contains the
    initial state. The last phase contains the final states.

    Between two consecutive phases i and i + 1, the automaton creates a set of
    arcs. For each transition (tail, transition, head), it will add an arc from
    the state 'tail' of phase i and the state 'head' of phase i + 1. This arc
    labeled by the value 'transition' of the variables 'variables[i]'. That is,
    this arc can only be selected if 'variables[i]' is assigned the value
    'transition'.

    A feasible solution of this constraint is an assignment of variables such
    that, starting from the initial state in phase 0, there is a path labeled by
    the values of the variables that ends in one of the final states in the
    final phase.

    Args:
      transition_variables: A non empty list of variables whose values
        correspond to the labels of the arcs traversed by the automata.
      starting_state: The initial state of the automata.
      final_states: A non empty list of admissible final states.
      transition_triples: A list of transition for the automata, in the
        following format (current_state, variable_value, next_state).

    Returns:
      An instance of the Constraint class.

    Raises:
      ValueError: if transition_variables, final_states, or transition_triples
      are empty.
    """

        if not transition_variables:
            raise ValueError(
                'AddAutomata expects a non empty transition_variables '
                'array')
        if not final_states:
            raise ValueError('AddAutomata expects some final states')

        if not transition_triples:
            raise ValueError('AddAutomata expects some transtion triples')

        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.automata.vars.extend(
            [self.GetOrMakeIndex(x) for x in transition_variables])
        cp_model_helper.AssertIsInt64(starting_state)
        model_ct.automata.starting_state = starting_state
        for v in final_states:
            cp_model_helper.AssertIsInt64(v)
            model_ct.automata.final_states.append(v)
        for t in transition_triples:
            if len(t) != 3:
                raise TypeError('Tuple ' + str(t) +
                                ' has the wrong arity (!= 3)')
            cp_model_helper.AssertIsInt64(t[0])
            cp_model_helper.AssertIsInt64(t[1])
            cp_model_helper.AssertIsInt64(t[2])
            model_ct.automata.transition_tail.append(t[0])
            model_ct.automata.transition_label.append(t[1])
            model_ct.automata.transition_head.append(t[2])
        return ct

    def AddInverse(self, variables, inverse_variables):
        """Adds Inverse(variables, inverse_variables).

    An inverse constraint enforces that if 'variables[i]' is assigned a value
    'j', then inverse_variables[j] is assigned a value 'i'. And vice versa.

    Args:
      variables: An array of integer variables.
      inverse_variables: An array of integer variables.

    Returns:
      An instance of the Constraint class.

    Raises:
      TypeError: if variables and inverse_variables have different length, or
          if they are empty.
    """

        if not variables or not inverse_variables:
            raise TypeError(
                'The Inverse constraint does not accept empty arrays')
        if len(variables) != len(inverse_variables):
            raise TypeError(
                'In the inverse constraint, the two array variables and'
                ' inverse_variables must have the same length.')
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.inverse.f_direct.extend(
            [self.GetOrMakeIndex(x) for x in variables])
        model_ct.inverse.f_inverse.extend(
            [self.GetOrMakeIndex(x) for x in inverse_variables])
        return ct

    def AddReservoirConstraint(self, times, demands, min_level, max_level):
        """Adds Reservoir(times, demands, min_level, max_level).

    Maintains a reservoir level within bounds. The water level starts at 0, and
    at any time >= 0, it must be between min_level and max_level. Furthermore,
    this constraints expect all times variables to be >= 0.
    If the variable times[i] is assigned a value t, then the current level
    changes by demands[i] (which is constant) at the time t.

    Note that level min can be > 0, or level max can be < 0. It just forces
    some demands to be executed at time 0 to make sure that we are within those
    bounds with the executed demands. Therefore, at any time t >= 0:
        sum(demands[i] if times[i] <= t) in [min_level, max_level]

    Args:
      times: A list of positive integer variables which specify the time of the
        filling or emptying the reservoir.
      demands: A list of integer values that specifies the amount of the
        emptying or feeling.
      min_level: At any time >= 0, the level of the reservoir must be greater of
        equal than the min level.
      max_level: At any time >= 0, the level of the reservoir must be less or
        equal than the max level.

    Returns:
      An instance of the Constraint class.

    Raises:
      ValueError: if max_level < min_level.
    """

        if max_level < min_level:
            return ValueError(
                'Reservoir constraint must have a max_level >= min_level')

        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.reservoir.times.extend([self.GetOrMakeIndex(x) for x in times])
        model_ct.reservoir.demands.extend(demands)
        model_ct.reservoir.min_level = min_level
        model_ct.reservoir.max_level = max_level
        return ct

    def AddReservoirConstraintWithActive(self, times, demands, actives,
                                         min_level, max_level):
        """Adds Reservoir(times, demands, actives, min_level, max_level).

    Maintain a reservoir level within bounds. The water level starts at 0, and
    at
    any time >= 0, it must be within min_level, and max_level. Furthermore, this
    constraints expect all times variables to be >= 0.
    If actives[i] is true, and if times[i] is assigned a value t, then the
    level of the reservoir changes by demands[i] (which is constant) at time t.

    Note that level_min can be > 0, or level_max can be < 0. It just forces
    some demands to be executed at time 0 to make sure that we are within those
    bounds with the executed demands. Therefore, at any time t >= 0:
        sum(demands[i] * actives[i] if times[i] <= t) in [min_level, max_level]
    The array of boolean variables 'actives', if defined, indicates which
    actions are actually performed.

    Args:
      times: A list of positive integer variables which specify the time of the
        filling or emptying the reservoir.
      demands: A list of integer values that specifies the amount of the
        emptying or feeling.
      actives: a list of boolean variables. They indicates if the
        emptying/refilling events actually take place.
      min_level: At any time >= 0, the level of the reservoir must be greater of
        equal than the min level.
      max_level: At any time >= 0, the level of the reservoir must be less or
        equal than the max level.

    Returns:
      An instance of the Constraint class.

    Raises:
      ValueError: if max_level < min_level.
    """

        if max_level < min_level:
            return ValueError(
                'Reservoir constraint must have a max_level >= min_level')

        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.reservoir.times.extend([self.GetOrMakeIndex(x) for x in times])
        model_ct.reservoir.demands.extend(demands)
        model_ct.reservoir.actives.extend(actives)
        model_ct.reservoir.min_level = min_level
        model_ct.reservoir.max_level = max_level
        return ct

    def AddMapDomain(self, var, bool_var_array, offset=0):
        """Adds var == i + offset <=> bool_var_array[i] == true for all i."""

        for i, bool_var in enumerate(bool_var_array):
            b_index = bool_var.Index()
            var_index = var.Index()
            model_ct = self.__model.constraints.add()
            model_ct.linear.vars.append(var_index)
            model_ct.linear.coeffs.append(1)
            model_ct.linear.domain.extend([offset + i, offset + i])
            model_ct.enforcement_literal.append(b_index)

            model_ct = self.__model.constraints.add()
            model_ct.linear.vars.append(var_index)
            model_ct.linear.coeffs.append(1)
            model_ct.enforcement_literal.append(-b_index - 1)
            if offset + i - 1 >= INT_MIN:
                model_ct.linear.domain.extend([INT_MIN, offset + i - 1])
            if offset + i + 1 <= INT_MAX:
                model_ct.linear.domain.extend([offset + i + 1, INT_MAX])

    def AddImplication(self, a, b):
        """Adds a => b."""
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.bool_or.literals.append(self.GetOrMakeBooleanIndex(b))
        model_ct.enforcement_literal.append(self.GetOrMakeBooleanIndex(a))
        return ct

    def AddBoolOr(self, literals):
        """Adds Or(literals) == true."""
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.bool_or.literals.extend(
            [self.GetOrMakeBooleanIndex(x) for x in literals])
        return ct

    def AddBoolAnd(self, literals):
        """Adds And(literals) == true."""
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.bool_and.literals.extend(
            [self.GetOrMakeBooleanIndex(x) for x in literals])
        return ct

    def AddBoolXOr(self, literals):
        """Adds XOr(literals) == true."""
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.bool_xor.literals.extend(
            [self.GetOrMakeBooleanIndex(x) for x in literals])
        return ct

    def AddMinEquality(self, target, variables):
        """Adds target == Min(variables)."""
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.int_min.vars.extend(
            [self.GetOrMakeIndex(x) for x in variables])
        model_ct.int_min.target = self.GetOrMakeIndex(target)
        return ct

    def AddMaxEquality(self, target, args):
        """Adds target == Max(variables)."""
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.int_max.vars.extend([self.GetOrMakeIndex(x) for x in args])
        model_ct.int_max.target = self.GetOrMakeIndex(target)
        return ct

    def AddDivisionEquality(self, target, num, denom):
        """Adds target == num // denom."""
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.int_div.vars.extend(
            [self.GetOrMakeIndex(num),
             self.GetOrMakeIndex(denom)])
        model_ct.int_div.target = self.GetOrMakeIndex(target)
        return ct

    def AddAbsEquality(self, target, var):
        """Adds target == Abs(var)."""
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        index = self.GetOrMakeIndex(var)
        model_ct.int_max.vars.extend([index, -index - 1])
        model_ct.int_max.target = self.GetOrMakeIndex(target)
        return ct

    def AddModuloEquality(self, target, var, mod):
        """Adds target = var % mod."""
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.int_mod.vars.extend(
            [self.GetOrMakeIndex(var),
             self.GetOrMakeIndex(mod)])
        model_ct.int_mod.target = self.GetOrMakeIndex(target)
        return ct

    def AddProdEquality(self, target, args):
        """Adds target == PROD(args)."""
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.int_prod.vars.extend([self.GetOrMakeIndex(x) for x in args])
        model_ct.int_prod.target = self.GetOrMakeIndex(target)
        return ct

    # Scheduling support

    def NewIntervalVar(self, start, size, end, name):
        """Creates an interval variable from start, size, and end.

    An interval variable is a constraint, that is itself used in other
    constraints like NoOverlap.

    Internally, it ensures that start + size == end.

    Args:
      start: The start of the interval. It can be an integer value, or an
        integer variable.
      size: The size of the interval. It can be an integer value, or an integer
        variable.
      end: The end of the interval. It can be an integer value, or an integer
        variable.
      name: The name of the interval variable.

    Returns:
      An IntervalVar object.
    """

        start_index = self.GetOrMakeIndex(start)
        size_index = self.GetOrMakeIndex(size)
        end_index = self.GetOrMakeIndex(end)
        return IntervalVar(self.__model, start_index, size_index, end_index,
                           None, name)

    def NewOptionalIntervalVar(self, start, size, end, is_present, name):
        """Creates an optional interval var from start, size, end and is_present.

    An optional interval variable is a constraint, that is itself used in other
    constraints like NoOverlap. This constraint is protected by an is_present
    literal that indicates if it is active or not.

    Internally, it ensures that is_present implies start + size == end.

    Args:
      start: The start of the interval. It can be an integer value, or an
        integer variable.
      size: The size of the interval. It can be an integer value, or an integer
        variable.
      end: The end of the interval. It can be an integer value, or an integer
        variable.
      is_present: A literal that indicates if the interval is active or not. A
        inactive interval is simply ignored by all constraints.
      name: The name of the interval variable.

    Returns:
      An IntervalVar object.
    """
        is_present_index = self.GetOrMakeBooleanIndex(is_present)
        start_index = self.GetOrMakeIndex(start)
        size_index = self.GetOrMakeIndex(size)
        end_index = self.GetOrMakeIndex(end)
        return IntervalVar(self.__model, start_index, size_index, end_index,
                           is_present_index, name)

    def AddNoOverlap(self, interval_vars):
        """Adds NoOverlap(interval_vars).

    A NoOverlap constraint ensures that all present intervals do not overlap
    in time.

    Args:
      interval_vars: The list of interval variables to constrain.

    Returns:
      An instance of the Constraint class.
    """
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.no_overlap.intervals.extend(
            [self.GetIntervalIndex(x) for x in interval_vars])
        return ct

    def AddNoOverlap2D(self, x_intervals, y_intervals):
        """Adds NoOverlap2D(x_intervals, y_intervals).

    A NoOverlap2D constraint ensures that all present rectangles do not overlap
    on a plan. Each rectangle is aligned with the X and Y axis, and is defined
    by two intervals which represent its projection onto the X and Y axis.

    Args:
      x_intervals: The X coordinates of the rectangles.
      y_intervals: The Y coordinates of the rectangles.

    Returns:
      An instance of the Constraint class.
    """
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.no_overlap_2d.x_intervals.extend(
            [self.GetIntervalIndex(x) for x in x_intervals])
        model_ct.no_overlap_2d.y_intervals.extend(
            [self.GetIntervalIndex(x) for x in y_intervals])
        return ct

    def AddCumulative(self, intervals, demands, capacity):
        """Adds Cumulative(intervals, demands, capacity).

    This constraint enforces that:
      for all t:
        sum(demands[i]
            if (start(intervals[t]) <= t < end(intervals[t])) and
            (t is present)) <= capacity

    Args:
      intervals: The list of intervals.
      demands: The list of demands for each interval. Each demand must be >= 0.
        Each demand can be an integer value, or an integer variable.
      capacity: The maximum capacity of the cumulative constraint. It must be a
        positive integer value or variable.

    Returns:
      An instance of the Constraint class.
    """
        ct = Constraint(self.__model.constraints)
        model_ct = self.__model.constraints[ct.Index()]
        model_ct.cumulative.intervals.extend(
            [self.GetIntervalIndex(x) for x in intervals])
        model_ct.cumulative.demands.extend(
            [self.GetOrMakeIndex(x) for x in demands])
        model_ct.cumulative.capacity = self.GetOrMakeIndex(capacity)
        return ct

    # Helpers.

    def __str__(self):
        return str(self.__model)

    def ModelProto(self):
        return self.__model

    def Negated(self, index):
        return -index - 1

    def GetOrMakeIndex(self, arg):
        """Returns the index of a variables, its negation, or a number."""
        if isinstance(arg, IntVar):
            return arg.Index()
        elif (isinstance(arg, _ProductCst) and
              isinstance(arg.Expression(), IntVar) and arg.Coefficient() == -1):
            return -arg.Expression().Index() - 1
        elif isinstance(arg, numbers.Integral):
            cp_model_helper.AssertIsInt64(arg)
            return self.GetOrMakeIndexFromConstant(arg)
        else:
            raise TypeError('NotSupported: model.GetOrMakeIndex(' + str(arg) +
                            ')')

    def GetOrMakeBooleanIndex(self, arg):
        """Returns an index from a boolean expression."""
        if isinstance(arg, IntVar):
            self.AssertIsBooleanVariable(arg)
            return arg.Index()
        elif isinstance(arg, _NotBooleanVariable):
            self.AssertIsBooleanVariable(arg.Not())
            return arg.Index()
        elif isinstance(arg, numbers.Integral):
            cp_model_helper.AssertIsBoolean(arg)
            return self.GetOrMakeIndexFromConstant(arg)
        else:
            raise TypeError('NotSupported: model.GetOrMakeBooleanIndex(' +
                            str(arg) + ')')

    def GetIntervalIndex(self, arg):
        if not isinstance(arg, IntervalVar):
            raise TypeError('NotSupported: model.GetIntervalIndex(%s)' % arg)
        return arg.Index()

    def GetOrMakeIndexFromConstant(self, value):
        if value in self.__constant_map:
            return self.__constant_map[value]
        index = len(self.__model.variables)
        var = self.__model.variables.add()
        var.domain.extend([value, value])
        self.__constant_map[value] = index
        return index

    def VarIndexToVarProto(self, var_index):
        if var_index > 0:
            return self.__model.variables[var_index]
        else:
            return self.__model.variables[-var_index - 1]

    def _SetObjective(self, obj, minimize):
        """Sets the objective of the model."""
        if isinstance(obj, IntVar):
            self.__model.ClearField('objective')
            self.__model.objective.coeffs.append(1)
            self.__model.objective.offset = 0
            if minimize:
                self.__model.objective.vars.append(obj.Index())
                self.__model.objective.scaling_factor = 1
            else:
                self.__model.objective.vars.append(self.Negated(obj.Index()))
                self.__model.objective.scaling_factor = -1
        elif isinstance(obj, LinearExpression):
            coeffs_map, constant = obj.GetVarValueMap()
            self.__model.ClearField('objective')
            if minimize:
                self.__model.objective.scaling_factor = 1
                self.__model.objective.offset = constant
            else:
                self.__model.objective.scaling_factor = -1
                self.__model.objective.offset = -constant
            for v, c, in iteritems(coeffs_map):
                self.__model.objective.coeffs.append(c)
                if minimize:
                    self.__model.objective.vars.append(v.Index())
                else:
                    self.__model.objective.vars.append(self.Negated(v.Index()))
        elif isinstance(obj, numbers.Integral):
            self.__model.objective.offset = obj
            self.__model.objective.scaling_factor = 1
        else:
            raise TypeError('TypeError: ' + str(obj) +
                            ' is not a valid objective')

    def Minimize(self, obj):
        """Sets the objective of the model to minimize(obj)."""
        self._SetObjective(obj, minimize=True)

    def Maximize(self, obj):
        """Sets the objective of the model to maximize(obj)."""
        self._SetObjective(obj, minimize=False)

    def HasObjective(self):
        return self.__model.HasField('objective')

    def AddDecisionStrategy(self, variables, var_strategy, domain_strategy):
        """Adds a search strategy to the model.

    Args:
      variables: a list of variables this strategy will assign.
      var_strategy: heuristic to choose the next variable to assign.
      domain_strategy: heuristic to reduce the domain of the selected variable.
        Currently, this is advanced code, the union of all strategies added to
        the model must be complete, i.e. instantiates all variables. Otherwise,
        Solve() will fail.
    """

        strategy = self.__model.search_strategy.add()
        for v in variables:
            strategy.variables.append(v.Index())
        strategy.variable_selection_strategy = var_strategy
        strategy.domain_reduction_strategy = domain_strategy

    def ModelStats(self):
        """Returns some statistics on the model as a string."""
        return pywrapsat.SatHelper.ModelStats(self.__model)

    def Validate(self):
        """Returns a string explaining the issue is the model is not valid."""
        return pywrapsat.SatHelper.ValidateModel(self.__model)

    def AssertIsBooleanVariable(self, x):
        if isinstance(x, IntVar):
            var = self.__model.variables[x.Index()]
            if len(var.domain) != 2 or var.domain[0] < 0 or var.domain[1] > 1:
                raise TypeError('TypeError: ' + str(x) +
                                ' is not a boolean variable')
        elif not isinstance(x, _NotBooleanVariable):
            raise TypeError('TypeError: ' + str(x) +
                            ' is not a boolean variable')


def EvaluateLinearExpression(expression, solution):
    """Evaluate an linear expression against a solution."""
    if isinstance(expression, numbers.Integral):
        return expression
    value = 0
    to_process = [(expression, 1)]
    while to_process:
        expr, coef = to_process.pop()
        if isinstance(expr, _ProductCst):
            to_process.append((expr.Expression(), coef * expr.Coefficient()))
        elif isinstance(expr, _SumArray):
            for e in expr.Array():
                to_process.append((e, coef))
            value += expr.Constant() * coef
        elif isinstance(expr, IntVar):
            value += coef * solution.solution[expr.Index()]
        elif isinstance(expr, _NotBooleanVariable):
            raise TypeError('Cannot interpret literals in a linear expression.')
    return value


def EvaluateBooleanExpression(literal, solution):
    """Evaluate an boolean expression against a solution."""
    if isinstance(literal, numbers.Integral):
        return bool(literal)
    elif isinstance(literal, IntVar) or isinstance(literal,
                                                   _NotBooleanVariable):
        index = literal.Index()
        if index >= 0:
            return bool(solution.solution[index])
        else:
            return not solution.solution[-index - 1]
    else:
        raise TypeError(
            'Cannot interpret %s as a boolean expression.' % literal)


class CpSolver(object):
    """Main solver class.

  The purpose of this class is to search for a solution of a model given to the
  Solve() method.

  Once Solve() is called, this class allows inspecting the solution found
  with the Value() and BooleanValue() methods, as well as general statistics
  about the solve procedure.
  """

    def __init__(self):
        self.__model = None
        self.__solution = None
        self.parameters = sat_parameters_pb2.SatParameters()

    def Solve(self, model):
        """Solves the given model and returns the solve status."""
        self.__solution = pywrapsat.SatHelper.SolveWithParameters(
            model.ModelProto(), self.parameters)
        return self.__solution.status

    def SolveWithSolutionCallback(self, model, callback):
        """Solves a problem and pass each solution found to the callback."""
        self.__solution = (
            pywrapsat.SatHelper.SolveWithParametersAndSolutionCallback(
                model.ModelProto(), self.parameters, callback))
        return self.__solution.status

    def SearchForAllSolutions(self, model, callback):
        """Search for all solutions of a satisfiability problem.

    This method searches for all feasible solution of a given model.
    Then it feeds the solution to the callback.

    Args:
      model: The model to solve.
      callback: The callback that will be called at each solution.

    Returns:
      The status of the solve (FEASIBLE, INFEASIBLE...).
    """
        if model.HasObjective():
            raise TypeError('Search for all solutions is only defined on '
                            'satisfiability problems')
        # Store old values.
        enumerate_all = self.parameters.enumerate_all_solutions
        self.parameters.enumerate_all_solutions = True
        self.__solution = (
            pywrapsat.SatHelper.SolveWithParametersAndSolutionCallback(
                model.ModelProto(), self.parameters, callback))
        # Restore parameters.
        self.parameters.enumerate_all_solutions = enumerate_all
        return self.__solution.status

    def Value(self, expression):
        """Returns the value of an linear expression after solve."""
        if not self.__solution:
            raise RuntimeError('Solve() has not be called.')
        return EvaluateLinearExpression(expression, self.__solution)

    def BooleanValue(self, literal):
        """Returns the boolean value of a literal after solve."""
        if not self.__solution:
            raise RuntimeError('Solve() has not be called.')
        return EvaluateBooleanExpression(literal, self.__solution)

    def ObjectiveValue(self):
        """Returns the value of objective after solve."""
        return self.__solution.objective_value

    def BestObjectiveBound(self):
        """Returns the best lower (upper) bound found when min(max)imizing."""
        return self.__solution.best_objective_bound

    def StatusName(self, status):
        """Returns the name of the status returned by Solve()."""
        return cp_model_pb2.CpSolverStatus.Name(status)

    def NumBooleans(self):
        """Returns the number of boolean variables managed by the SAT solver."""
        return self.__solution.num_booleans

    def NumConflicts(self):
        """Returns the number of conflicts since the creation of the solver."""
        return self.__solution.num_conflicts

    def NumBranches(self):
        """Returns the number of search branches explored by the solver."""
        return self.__solution.num_branches

    def WallTime(self):
        """Returns the wall time in seconds since the creation of the solver."""
        return self.__solution.wall_time

    def UserTime(self):
        """Returns the user time in seconds since the creation of the solver."""
        return self.__solution.user_time

    def ResponseStats(self):
        """Returns some statistics on the solution found as a string."""
        return pywrapsat.SatHelper.SolverResponseStats(self.__solution)


class CpSolverSolutionCallback(pywrapsat.SolutionCallback):
    """Solution callback.

  This class implements a callback that will be called at each new solution
  found during search.

  The method OnSolutionCallback() will be called by the solver, and must be
  implemented. The current solution can be queried using the BooleanValue()
  and Value() methods.
  """

    def OnSolutionCallback(self):
        """Proxy to the same method in snake case."""
        self.on_solution_callback()

    def BooleanValue(self, lit):
        """Returns the boolean value of a boolean literal.

    Args:
        lit: A boolean variable or its negation.

    Returns:
        The boolean value of the literal in the solution.

    Raises:
        RuntimeError: if 'lit' is not a boolean variable or its negation.
    """
        if not self.Response().solution:
            raise RuntimeError('Solve() has not be called.')
        if isinstance(lit, numbers.Integral):
            return bool(lit)
        elif isinstance(lit, IntVar) or isinstance(lit, _NotBooleanVariable):
            index = lit.Index()
            return self.SolutionBooleanValue(index)
        else:
            raise TypeError(
                'Cannot interpret %s as a boolean expression.' % lit)

    def Value(self, expression):
        """Evaluates an linear expression in the current solution.

    Args:
        expression: a linear expression of the model.

    Returns:
        An integer value equal to the evaluation of the linear expression
        against the current solution.

    Raises:
        RuntimeError: if 'expression' is not a LinearExpression.
    """
        if not self.Response().solution:
            raise RuntimeError('Solve() has not be called.')
        if isinstance(expression, numbers.Integral):
            return expression
        value = 0
        to_process = [(expression, 1)]
        while to_process:
            expr, coef = to_process.pop()
            if isinstance(expr, _ProductCst):
                to_process.append((expr.Expression(),
                                   coef * expr.Coefficient()))
            elif isinstance(expr, _SumArray):
                for e in expr.Array():
                    to_process.append((e, coef))
                    value += expr.Constant() * coef
            elif isinstance(expr, IntVar):
                value += coef * self.SolutionIntegerValue(expr.Index())
            elif isinstance(expr, _NotBooleanVariable):
                raise TypeError(
                    'Cannot interpret literals in a linear expression.')
        return value


class ObjectiveSolutionPrinter(CpSolverSolutionCallback):
    """Print intermediate solutions objective and time."""

    def __init__(self):
        CpSolverSolutionCallback.__init__(self)
        self.__solution_count = 0
        self.__start_time = time.time()

    def on_solution_callback(self):
        """Called on each new solution."""
        current_time = time.time()
        objective = self.ObjectiveValue()
        print('Solution %i, time = %f s, objective = [%i, %i]' %
              (self.__solution_count, current_time - self.__start_time,
               objective, self.BestObjectiveBound()))
        self.__solution_count += 1
