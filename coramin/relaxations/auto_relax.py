import pyomo.environ as pe
from pyomo.core.kernel.component_map import ComponentMap
import pyomo.core.expr.numeric_expr as numeric_expr
from pyomo.core.expr.visitor import ExpressionValueVisitor, identify_variables
from pyomo.core.expr.numvalue import nonpyomo_leaf_types, value
from pyomo.core.expr.numvalue import is_fixed, polynomial_degree, is_constant
from pyomo.contrib.fbbt.fbbt import compute_bounds_on_expr, fbbt
import math
from pyomo.core.base.constraint import Constraint
import logging
from .univariate import PWUnivariateRelaxation, PWXSquaredRelaxation, PWCosRelaxation, PWSinRelaxation, PWArctanRelaxation
from .pw_mccormick import PWMcCormickRelaxation
from coramin.utils.coramin_enums import RelaxationSide, FunctionShape
from pyomo.gdp import Disjunct

logger = logging.getLogger(__name__)


class RelaxationException(Exception):
    pass


class RelaxationCounter(object):
    def __init__(self):
        self.count = 0

    def increment(self):
        self.count += 1

    def __str__(self):
        return str(self.count)


def replace_sub_expression_with_aux_var(arg, parent_block):
    if type(arg) in nonpyomo_leaf_types:
        return arg
    elif arg.is_expression_type():
        _var = parent_block.aux_vars.add()
        _con = parent_block.aux_cons.add(_var == arg)
        fbbt(_con)
        return _var
    else:
        return arg


def _get_aux_var(parent_block, expr):
    _aux_var = parent_block.aux_vars.add()
    lb, ub = compute_bounds_on_expr(expr)
    _aux_var.setlb(lb)
    _aux_var.setub(ub)
    return _aux_var


def _relax_leaf_to_root_ProductExpression(node, values, aux_var_map, degree_map, parent_block, relaxation_side_map, counter):
    arg1, arg2 = values

    # The purpose of the next bit of code is to find common quadratic terms. For example, suppose we are relaxing
    # a model with the following two constraints:
    #
    # w1 - x*y = 0
    # w2 + 3*x*y = 0
    #
    # we want to end up with
    #
    # w1 - aux1 = 0
    # w2 + 3*aux1 = 0
    # aux1 = x*y
    #
    # rather than
    #
    # w1 - aux1 = 0
    # w2 + 3*aux2 = 0
    # aux1 = x*y
    # aux2 = x*y
    #

    if arg1.__class__ == numeric_expr.MonomialTermExpression:
        coef, arg1 = arg1.args
    elif arg2.__class__ == numeric_expr.MonomialTermExpression:
        coef, arg2 = arg2.args
    else:
        coef = None
    degree_1 = degree_map[arg1]
    degree_2 = degree_map[arg2]
    if degree_1 == 0 or degree_2 == 0:
        res = arg1 * arg2
        if coef is not None:
            res = coef*res
        degree_map[res] = degree_1 + degree_2
        return res
    elif arg1 is arg2:
        # reformulate arg1 * arg2 as arg1**2
        _new_relaxation_side_map = ComponentMap()
        if coef is None:
            _reformulated = arg1**2
        else:
            _reformulated = coef * arg1**2
        _new_relaxation_side_map[_reformulated] = relaxation_side_map[node]
        res = _relax_expr(expr=_reformulated, aux_var_map=aux_var_map, parent_block=parent_block,
                          relaxation_side_map=_new_relaxation_side_map, counter=counter)
        degree_map[res] = 1
        return res
    elif (id(arg1), id(arg2), 'mul') in aux_var_map or (id(arg2), id(arg1), 'mul') in aux_var_map:
        if (id(arg1), id(arg2), 'mul') in aux_var_map:
            _aux_var, relaxation = aux_var_map[id(arg1), id(arg2), 'mul']
        else:
            _aux_var, relaxation = aux_var_map[id(arg2), id(arg1), 'mul']
        relaxation_side = relaxation_side_map[node]
        if relaxation_side != relaxation.relaxation_side:
            relaxation.relaxation_side = RelaxationSide.BOTH
        if coef is not None:
            res = coef * _aux_var
            degree_map[_aux_var] = 1
            degree_map[res] = 1
        else:
            res = _aux_var
            degree_map[res] = 1
        return res
    else:
        _aux_var = _get_aux_var(parent_block, arg1 * arg2)
        arg1 = replace_sub_expression_with_aux_var(arg1, parent_block)
        arg2 = replace_sub_expression_with_aux_var(arg2, parent_block)
        relaxation_side = relaxation_side_map[node]
        relaxation = PWMcCormickRelaxation()
        relaxation.set_input(x=arg1, y=arg2, w=_aux_var, relaxation_side=relaxation_side)
        aux_var_map[id(arg1), id(arg2), 'mul'] = (_aux_var, relaxation)
        setattr(parent_block.relaxations, 'rel'+str(counter), relaxation)
        counter.increment()
        if coef is not None:
            res = coef * _aux_var
            degree_map[_aux_var] = 1
            degree_map[res] = 1
        else:
            res = _aux_var
            degree_map[res] = 1
        return res


def _relax_leaf_to_root_ReciprocalExpression(node, values, aux_var_map, degree_map, parent_block, relaxation_side_map, counter):
    arg = values[0]
    degree = degree_map[arg]
    if degree == 0:
        res = 1/arg
        degree_map[res] = 0
        return res
    elif (id(arg), 'reciprocal') in aux_var_map:
        _aux_var, relaxation = aux_var_map[id(arg), 'reciprocal']
        relaxation_side = relaxation_side_map[node]
        if relaxation_side != relaxation.relaxation_side:
            relaxation.relaxation_side = RelaxationSide.BOTH
        return _aux_var
    else:
        _aux_var = _get_aux_var(parent_block, 1/arg)
        arg = replace_sub_expression_with_aux_var(arg, parent_block)
        relaxation_side = relaxation_side_map[node]
        degree_map[_aux_var] = 1
        if compute_bounds_on_expr(arg)[0] > 0:
            relaxation = PWUnivariateRelaxation()
            relaxation.set_input(x=arg, w=_aux_var, relaxation_side=relaxation_side, f_x_expr=1/arg,
                                 shape=FunctionShape.CONVEX)
        elif compute_bounds_on_expr(arg)[1] < 0:
            relaxation = PWUnivariateRelaxation()
            relaxation.set_input(x=arg, w=_aux_var, relaxation_side=relaxation_side, f_x_expr=1/arg,
                                 shape=FunctionShape.CONCAVE)
        else:
            _one = parent_block.aux_vars.add()
            _one.fix(1.0)
            relaxation = PWMcCormickRelaxation()
            relaxation.set_input(x=arg, y=_aux_var, w=_one, relaxation_side=relaxation_side)
        aux_var_map[id(arg), 'reciprocal'] = (_aux_var, relaxation)
        setattr(parent_block.relaxations, 'rel'+str(counter), relaxation)
        counter.increment()
        return _aux_var


def _relax_quadratic(arg1, aux_var_map, relaxation_side, degree_map, parent_block, counter):
    if (id(arg1), 'quadratic') in aux_var_map:
        _aux_var, relaxation = aux_var_map[id(arg1), 'quadratic']
        if relaxation_side != relaxation.relaxation_side:
            relaxation.relaxation_side = RelaxationSide.BOTH
        degree_map[_aux_var] = 1
        return _aux_var
    else:
        _aux_var = _get_aux_var(parent_block, arg1**2)
        arg1 = replace_sub_expression_with_aux_var(arg1, parent_block)
        degree_map[_aux_var] = 1
        relaxation = PWXSquaredRelaxation()
        relaxation.set_input(x=arg1, w=_aux_var, relaxation_side=relaxation_side)
        aux_var_map[id(arg1), 'quadratic'] = (_aux_var, relaxation)
        setattr(parent_block.relaxations, 'rel' + str(counter), relaxation)
        counter.increment()
        return _aux_var


def _relax_convex_pow(arg1, arg2, aux_var_map, relaxation_side, degree_map, parent_block, counter, swap=False):
    if (id(arg1), id(arg2), 'pow') in aux_var_map:
        _aux_var, relaxation = aux_var_map[id(arg1), id(arg2), 'pow']
        if relaxation_side != relaxation.relaxation_side:
            relaxation.relaxation_side = RelaxationSide.BOTH
        degree_map[_aux_var] = 1
        return _aux_var
    else:
        _aux_var = _get_aux_var(parent_block, arg1**arg2)
        if swap:
            arg2 = replace_sub_expression_with_aux_var(arg2, parent_block)
            _x = arg2
        else:
            arg1 = replace_sub_expression_with_aux_var(arg1, parent_block)
            _x = arg1
        degree_map[_aux_var] = 1
        relaxation = PWUnivariateRelaxation()
        relaxation.set_input(x=_x, w=_aux_var, relaxation_side=relaxation_side, f_x_expr=arg1 ** arg2,
                             shape=FunctionShape.CONVEX)
        aux_var_map[id(arg1), id(arg2), 'pow'] = (_aux_var, relaxation)
        setattr(parent_block.relaxations, 'rel' + str(counter), relaxation)
        counter.increment()
        return _aux_var


def _relax_concave_pow(arg1, arg2, aux_var_map, relaxation_side, degree_map, parent_block, counter):
    if (id(arg1), id(arg2), 'pow') in aux_var_map:
        _aux_var, relaxation = aux_var_map[id(arg1), id(arg2), 'pow']
        if relaxation_side != relaxation.relaxation_side:
            relaxation.relaxation_side = RelaxationSide.BOTH
        degree_map[_aux_var] = 1
        return _aux_var
    else:
        _aux_var = _get_aux_var(parent_block, arg1 ** arg2)
        arg1 = replace_sub_expression_with_aux_var(arg1, parent_block)
        degree_map[_aux_var] = 1
        relaxation = PWUnivariateRelaxation()
        relaxation.set_input(x=arg1, w=_aux_var, relaxation_side=relaxation_side, f_x_expr=arg1 ** arg2,
                             shape=FunctionShape.CONCAVE)
        aux_var_map[id(arg1), id(arg2), 'pow'] = (_aux_var, relaxation)
        setattr(parent_block.relaxations, 'rel' + str(counter), relaxation)
        counter.increment()
        return _aux_var


def _relax_leaf_to_root_PowExpression(node, values, aux_var_map, degree_map, parent_block, relaxation_side_map, counter):
    arg1, arg2 = values
    degree1 = degree_map[arg1]
    degree2 = degree_map[arg2]
    if degree2 == 0:
        if degree1 == 0:
            res = arg1 ** arg2
            degree_map[res] = 0
            return res
        if not is_constant(arg2):
            logger.warning('Only constant exponents are supported: ' + str(arg1**arg2) + '\nReplacing ' + str(arg2) + ' with its value.')
        arg2 = pe.value(arg2)
        if arg2 == 1:
            return arg1
        elif arg2 == 0:
            res = 1
            degree_map[res] = 0
            return res
        elif arg2 == 2:
            return _relax_quadratic(arg1=arg1, aux_var_map=aux_var_map, relaxation_side=relaxation_side_map[node],
                                    degree_map=degree_map, parent_block=parent_block, counter=counter)
        elif arg2 >= 0:
            if arg2 == round(arg2):
                if arg2 % 2 == 0 or compute_bounds_on_expr(arg1)[0] >= 0:
                    return _relax_convex_pow(arg1=arg1, arg2=arg2, aux_var_map=aux_var_map,
                                             relaxation_side=relaxation_side_map[node], degree_map=degree_map,
                                             parent_block=parent_block, counter=counter)
                elif compute_bounds_on_expr(arg1)[1] <= 0:
                    return _relax_concave_pow(arg1=arg1, arg2=arg2, aux_var_map=aux_var_map,
                                              relaxation_side=relaxation_side_map[node], degree_map=degree_map,
                                              parent_block=parent_block, counter=counter)
                else:  # reformulate arg1 ** arg2 as arg1 * arg1 ** (arg2 - 1)
                    _new_relaxation_side_map = ComponentMap()
                    _reformulated = arg1 * arg1 ** (arg2 - 1)
                    _new_relaxation_side_map[_reformulated] = relaxation_side_map[node]
                    res = _relax_expr(expr=_reformulated, aux_var_map=aux_var_map, parent_block=parent_block,
                                      relaxation_side_map=_new_relaxation_side_map, counter=counter)
                    degree_map[res] = 1
                    return res
            else:
                assert compute_bounds_on_expr(arg1)[0] >= 0
                if arg2 < 1:
                    return _relax_concave_pow(arg1=arg1, arg2=arg2, aux_var_map=aux_var_map,
                                              relaxation_side=relaxation_side_map[node], degree_map=degree_map,
                                              parent_block=parent_block, counter=counter)
                else:
                    return _relax_convex_pow(arg1=arg1, arg2=arg2, aux_var_map=aux_var_map,
                                             relaxation_side=relaxation_side_map[node], degree_map=degree_map,
                                             parent_block=parent_block, counter=counter)
        else:
            if arg2 == round(arg2):
                if compute_bounds_on_expr(arg1)[0] >= 0:
                    return _relax_convex_pow(arg1=arg1, arg2=arg2, aux_var_map=aux_var_map,
                                             relaxation_side=relaxation_side_map[node], degree_map=degree_map,
                                             parent_block=parent_block, counter=counter)
                elif compute_bounds_on_expr(arg1)[1] <= 0:
                    if arg2 % 2 == 0:
                        return _relax_convex_pow(arg1=arg1, arg2=arg2, aux_var_map=aux_var_map,
                                                 relaxation_side=relaxation_side_map[node], degree_map=degree_map,
                                                 parent_block=parent_block, counter=counter)
                    else:
                        return _relax_concave_pow(arg1=arg1, arg2=arg2, aux_var_map=aux_var_map,
                                                  relaxation_side=relaxation_side_map[node], degree_map=degree_map,
                                                  parent_block=parent_block, counter=counter)
                else:
                    # reformulate arg1 ** arg2 as 1 / arg1 ** (-arg2)
                    _new_relaxation_side_map = ComponentMap()
                    _reformulated = 1 / (arg1 ** (-arg2))
                    _new_relaxation_side_map[_reformulated] = relaxation_side_map[node]
                    res = _relax_expr(expr=_reformulated, aux_var_map=aux_var_map, parent_block=parent_block,
                                      relaxation_side_map=_new_relaxation_side_map, counter=counter)
                    degree_map[res] = 1
                    return res
            else:
                assert compute_bounds_on_expr(arg1)[0] >= 0
                return _relax_convex_pow(arg1=arg1, arg2=arg2, aux_var_map=aux_var_map,
                                         relaxation_side=relaxation_side_map[node], degree_map=degree_map,
                                         parent_block=parent_block, counter=counter)
    elif degree1 == 0:
        if not is_constant(arg1):
            logger.warning('Found {0} raised to a variable power. However, {0} does not appear to be constant (maybe '
                           'it is or depends on a mutable Param?). Replacing {0} with its value.'.format(str(arg1)))
            arg1 = pe.value(arg1)
        if arg1 < 0:
            raise ValueError('Cannot raise a negative base to a variable exponent: ' + str(arg1**arg2))
        return _relax_convex_pow(arg1=arg1, arg2=arg2, aux_var_map=aux_var_map,
                                 relaxation_side=relaxation_side_map[node], degree_map=degree_map,
                                 parent_block=parent_block, counter=counter, swap=True)
    else:
        if (id(arg1), id(arg2), 'pow') in aux_var_map:
            _aux_var, relaxation = aux_var_map[id(arg1), id(arg2), 'pow']
            if relaxation_side_map[node] != relaxation.relaxation_side:
                relaxation.relaxation_side = RelaxationSide.BOTH
            return _aux_var
        else:
            assert compute_bounds_on_expr(arg1)[0] >= 0
            _new_relaxation_side_map = ComponentMap()
            _reformulated = pe.exp(arg2 * pe.log(arg1))
            _new_relaxation_side_map[_reformulated] = relaxation_side_map[node]
            res = _relax_expr(expr=_reformulated, aux_var_map=aux_var_map, parent_block=parent_block,
                              relaxation_side_map=_new_relaxation_side_map, counter=counter)
            degree_map[res] = 1
            return res


def _relax_leaf_to_root_SumExpression(node, values, aux_var_map, degree_map, parent_block, relaxation_side_map, counter):
    res = sum(values)
    degree_map[res] = max([degree_map[arg] for arg in values])
    return res


def _relax_leaf_to_root_NegationExpression(node, values, aux_var_map, degree_map, parent_block, relaxation_side_map, counter):
    arg = values[0]
    res = -arg
    degree_map[res] = degree_map[arg]
    return res


def _relax_leaf_to_root_exp(node, values, aux_var_map, degree_map, parent_block, relaxation_side_map, counter):
    arg = values[0]
    degree = degree_map[arg]
    if degree == 0:
        res = pe.exp(arg)
        degree_map[res] = 0
        return res
    elif (id(arg), 'exp') in aux_var_map:
        _aux_var, relaxation = aux_var_map[id(arg), 'exp']
        relaxation_side = relaxation_side_map[node]
        if relaxation_side != relaxation.relaxation_side:
            relaxation.relaxation_side = RelaxationSide.BOTH
        degree_map[_aux_var] = 1
        return _aux_var
    else:
        _aux_var = _get_aux_var(parent_block, pe.exp(arg))
        arg = replace_sub_expression_with_aux_var(arg, parent_block)
        relaxation_side = relaxation_side_map[node]
        degree_map[_aux_var] = 1
        relaxation = PWUnivariateRelaxation()
        relaxation.set_input(x=arg, w=_aux_var, relaxation_side=relaxation_side, f_x_expr=pe.exp(arg),
                             shape=FunctionShape.CONVEX)
        aux_var_map[id(arg), 'exp'] = (_aux_var, relaxation)
        setattr(parent_block.relaxations, 'rel'+str(counter), relaxation)
        counter.increment()
        return _aux_var


def _relax_leaf_to_root_log(node, values, aux_var_map, degree_map, parent_block, relaxation_side_map, counter):
    arg = values[0]
    degree = degree_map[arg]
    if degree == 0:
        res = pe.exp(arg)
        degree_map[res] = 0
        return res
    elif (id(arg), 'log') in aux_var_map:
        _aux_var, relaxation = aux_var_map[id(arg), 'log']
        relaxation_side = relaxation_side_map[node]
        if relaxation_side != relaxation.relaxation_side:
            relaxation.relaxation_side = RelaxationSide.BOTH
        degree_map[_aux_var] = 1
        return _aux_var
    else:
        _aux_var = _get_aux_var(parent_block, pe.log(arg))
        arg = replace_sub_expression_with_aux_var(arg, parent_block)
        relaxation_side = relaxation_side_map[node]
        degree_map[_aux_var] = 1
        relaxation = PWUnivariateRelaxation()
        relaxation.set_input(x=arg, w=_aux_var, relaxation_side=relaxation_side, f_x_expr=pe.log(arg),
                             shape=FunctionShape.CONCAVE)
        aux_var_map[id(arg), 'log'] = (_aux_var, relaxation)
        setattr(parent_block.relaxations, 'rel'+str(counter), relaxation)
        counter.increment()
        return _aux_var


_unary_leaf_to_root_map = dict()
_unary_leaf_to_root_map['exp'] = _relax_leaf_to_root_exp
_unary_leaf_to_root_map['log'] = _relax_leaf_to_root_log


def _relax_leaf_to_root_UnaryFunctionExpression(node, values, aux_var_map, degree_map, parent_block, relaxation_side_map, counter):
    if node.getname() in _unary_leaf_to_root_map:
        return _unary_leaf_to_root_map[node.getname()](node=node, values=values, aux_var_map=aux_var_map,
                                                       degree_map=degree_map, parent_block=parent_block,
                                                       relaxation_side_map=relaxation_side_map, counter=counter)
    else:
        raise NotImplementedError('Cannot automatically relax ' + str(node))


_relax_leaf_to_root_map = dict()
_relax_leaf_to_root_map[numeric_expr.ProductExpression] = _relax_leaf_to_root_ProductExpression
_relax_leaf_to_root_map[numeric_expr.SumExpression] = _relax_leaf_to_root_SumExpression
_relax_leaf_to_root_map[numeric_expr.MonomialTermExpression] = _relax_leaf_to_root_ProductExpression
_relax_leaf_to_root_map[numeric_expr.NegationExpression] = _relax_leaf_to_root_NegationExpression
_relax_leaf_to_root_map[numeric_expr.PowExpression] = _relax_leaf_to_root_PowExpression
_relax_leaf_to_root_map[numeric_expr.ReciprocalExpression] = _relax_leaf_to_root_ReciprocalExpression
_relax_leaf_to_root_map[numeric_expr.UnaryFunctionExpression] = _relax_leaf_to_root_UnaryFunctionExpression


def _relax_root_to_leaf_ProductExpression(node, relaxation_side_map):
    arg1, arg2 = node.args
    relaxation_side_map[arg1] = RelaxationSide.BOTH
    relaxation_side_map[arg2] = RelaxationSide.BOTH


def _relax_root_to_leaf_ReciprocalExpression(node, relaxation_side_map):
    arg = node.args[0]
    relaxation_side_map[arg] = RelaxationSide.BOTH


def _relax_root_to_leaf_SumExpression(node, relaxation_side_map):
    relaxation_side = relaxation_side_map[node]

    for arg in node.args:
        relaxation_side_map[arg] = relaxation_side


def _relax_root_to_leaf_NegationExpression(node, relaxation_side_map):
    arg = node.args[0]
    relaxation_side = relaxation_side_map[node]
    if relaxation_side == RelaxationSide.BOTH:
        relaxation_side_map[arg] = RelaxationSide.BOTH
    elif relaxation_side == RelaxationSide.UNDER:
        relaxation_side_map[arg] = RelaxationSide.OVER
    else:
        assert relaxation_side == RelaxationSide.OVER
        relaxation_side_map[arg] = RelaxationSide.UNDER


def _relax_root_to_leaf_PowExpression(node, relaxations_side_map):
    arg1, arg2 = node.args
    relaxations_side_map[arg1] = RelaxationSide.BOTH
    relaxations_side_map[arg2] = RelaxationSide.BOTH


def _relax_root_to_leaf_exp(node, relaxation_side_map):
    arg = node.args[0]
    relaxation_side_map[arg] = relaxation_side_map[node]


def _relax_root_to_leaf_log(node, relaxation_side_map):
    arg = node.args[0]
    relaxation_side_map[arg] = relaxation_side_map[node]


_unary_root_to_leaf_map = dict()
_unary_root_to_leaf_map['exp'] = _relax_root_to_leaf_exp
_unary_root_to_leaf_map['log'] = _relax_root_to_leaf_log


def _relax_root_to_leaf_UnaryFunctionExpression(node, relaxation_side_map):
    if node.getname() in _unary_root_to_leaf_map:
        _unary_root_to_leaf_map[node.getname()](node, relaxation_side_map)
    else:
        raise NotImplementedError('Cannot automatically relax ' + str(node))


_relax_root_to_leaf_map = dict()
_relax_root_to_leaf_map[numeric_expr.ProductExpression] = _relax_root_to_leaf_ProductExpression
_relax_root_to_leaf_map[numeric_expr.SumExpression] = _relax_root_to_leaf_SumExpression
_relax_root_to_leaf_map[numeric_expr.MonomialTermExpression] = _relax_root_to_leaf_ProductExpression
_relax_root_to_leaf_map[numeric_expr.NegationExpression] = _relax_root_to_leaf_NegationExpression
_relax_root_to_leaf_map[numeric_expr.PowExpression] = _relax_root_to_leaf_PowExpression
_relax_root_to_leaf_map[numeric_expr.ReciprocalExpression] = _relax_root_to_leaf_ReciprocalExpression
_relax_root_to_leaf_map[numeric_expr.UnaryFunctionExpression] = _relax_root_to_leaf_UnaryFunctionExpression


class _FactorableRelaxationVisitor(ExpressionValueVisitor):
    """
    This walker generates new constraints with nonlinear terms replaced by
    auxiliary variables, and relaxations relating the auxilliary variables to
    the original variables.
    """
    def __init__(self, aux_var_map, parent_block, relaxation_side_map, counter):
        self.aux_var_map = aux_var_map
        self.parent_block = parent_block
        self.relaxation_side_map = relaxation_side_map
        self.counter = counter
        self.degree_map = ComponentMap()

    def visit(self, node, values):
        if node.__class__ in _relax_leaf_to_root_map:
            res = _relax_leaf_to_root_map[node.__class__](node, values, self.aux_var_map, self.degree_map,
                                                          self.parent_block, self.relaxation_side_map, self.counter)
            return res
        else:
            raise NotImplementedError('Cannot relax an expression of type ' + str(type(node)))

    def visiting_potential_leaf(self, node):
        if node.__class__ in nonpyomo_leaf_types:
            self.degree_map[node] = 0
            return True, node

        if node.is_variable_type():
            self.degree_map[node] = 1
            return True, node

        if not node.is_expression_type():
            self.degree_map[node] = 0
            return True, node

        if node.__class__ in _relax_root_to_leaf_map:
            _relax_root_to_leaf_map[node.__class__](node, self.relaxation_side_map)

        return False, None


def _relax_expr(expr, aux_var_map, parent_block, relaxation_side_map, counter):
    visitor = _FactorableRelaxationVisitor(aux_var_map=aux_var_map, parent_block=parent_block,
                                           relaxation_side_map=relaxation_side_map, counter=counter)
    new_expr = visitor.dfs_postorder_stack(expr)
    return new_expr


def relax(model, descend_into=None, in_place=False, use_fbbt=True):
    if not in_place:
        m = model.clone()
    else:
        m = model
    if use_fbbt:
        fbbt(m, deactivate_satisfied_constraints=True)

    if descend_into is None:
        descend_into = (pe.Block, Disjunct)

    aux_var_map = dict()
    counter_dict = dict()

    for c in m.component_data_objects(ctype=Constraint, active=True, descend_into=descend_into, sort=True):
        body_degree = polynomial_degree(c.body)
        if body_degree is not None:
            if body_degree <= 1:
                continue

        if c.lower is not None and c.upper is not None:
            relaxation_side = RelaxationSide.BOTH
        elif c.lower is not None:
            relaxation_side = RelaxationSide.OVER
        elif c.upper is not None:
            relaxation_side = RelaxationSide.UNDER
        else:
            raise ValueError('Encountered a constraint without a lower or an upper bound: ' + str(c))

        parent_block = c.parent_block()
        relaxation_side_map = ComponentMap()
        relaxation_side_map[c.body] = relaxation_side

        if parent_block in counter_dict:
            counter = counter_dict[parent_block]
        else:
            parent_block.relaxations = pe.Block()
            parent_block.aux_vars = pe.VarList()
            parent_block.aux_cons = pe.ConstraintList()
            counter = RelaxationCounter()
            counter_dict[parent_block] = counter

        new_body = _relax_expr(expr=c.body, aux_var_map=aux_var_map, parent_block=parent_block,
                               relaxation_side_map=relaxation_side_map, counter=counter)
        lb = c.lower
        ub = c.upper
        parent_block.aux_cons.add(pe.inequality(lb, new_body, ub))
        parent_component = c.parent_component()
        if parent_component.is_indexed():
            del parent_component[c.index()]
        else:
            parent_block.del_component(c)

    for c in m.component_data_objects(ctype=pe.Objective, active=True, descend_into=descend_into, sort=True):
        degree = polynomial_degree(c.expr)
        if degree is not None:
            if degree <= 1:
                continue

        if c.sense == pe.minimize:
            relaxation_side = RelaxationSide.UNDER
        elif c.sense == pe.maximize:
            relaxation_side = RelaxationSide.OVER
        else:
            raise ValueError('Encountered an objective with an unrecognized sense: ' + str(c))

        parent_block = c.parent_block()
        relaxation_side_map = ComponentMap()
        relaxation_side_map[c.expr] = relaxation_side

        if parent_block in counter_dict:
            counter = counter_dict[parent_block]
        else:
            parent_block.relaxations = pe.Block()
            parent_block.aux_vars = pe.VarList()
            parent_block.aux_cons = pe.ConstraintList()
            counter = RelaxationCounter()
            counter_dict[parent_block] = counter

        if not hasattr(parent_block, 'aux_objectives'):
            parent_block.aux_objectives = pe.ObjectiveList()

        new_body = _relax_expr(expr=c.expr, aux_var_map=aux_var_map, parent_block=parent_block,
                               relaxation_side_map=relaxation_side_map, counter=counter)
        sense = c.sense
        parent_block.aux_objectives.add(new_body, sense=sense)
        parent_component = c.parent_component()
        if parent_component.is_indexed():
            del parent_component[c.index()]
        else:
            parent_block.del_component(c)

    for _aux_var, relaxation in aux_var_map.values():
        relaxation.use_linear_relaxation = True
        relaxation.rebuild()

    return m
