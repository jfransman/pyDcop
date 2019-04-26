# BSD-3-Clause License
#
# Copyright 2017 Orange
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors
#    may be used to endorse or promote products derived from this software
#    without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.


"""

DPOP: Dynamic Programming Optimization Protocol
-----------------------------------------------

Dynamic Programming Optimization Protocol  is an optimal,
inference-based, dcop algorithm implementing a dynamic programming procedure
in a distributed way :cite:`petcu_distributed_2004`.

DPOP works on a Pseudo-tree, which can be built using the
:ref:`distribute<pydcop_commands_distribute>` command
(and is automatically built when using the :ref:`solve<pydcop_commands_solve>` command).

This algorithm has no parameter.


Example
^^^^^^^
::

    pydcop -algo dpop graph_coloring_eq.yaml


"""
from random import choice
from typing import Iterable

from pydcop.infrastructure.computations import Message, VariableComputation, register
from pydcop.dcop.objects import Variable
from pydcop.dcop.relations import (
    NAryMatrixRelation,
    Constraint,
    find_arg_optimal,
    join_utils, projection)
from pydcop.algorithms import ALGO_STOP, ALGO_CONTINUE, ComputationDef

GRAPH_TYPE = "pseudotree"


def build_computation(comp_def: ComputationDef):

    parent = None
    children = []
    for l in comp_def.node.links:
        if l.type == "parent" and l.source == comp_def.node.name:
            parent = l.target
        if l.type == "children" and l.source == comp_def.node.name:
            children.append(l.target)

    constraints = [r for r in comp_def.node.constraints]

    computation = DpopAlgo(
        comp_def.node.variable, parent, children, constraints, comp_def=comp_def
    )
    return computation


def computation_memory(*args):
    raise NotImplementedError("DPOP has no computation memory implementation (yet)")


def communication_load(*args):
    raise NotImplementedError("DPOP has no communication_load implementation (yet)")


class DpopMessage(Message):
    def __init__(self, msg_type, content):
        super(DpopMessage, self).__init__(msg_type, content)

    @property
    def size(self):
        # Dpop messages
        # UTIL : multi-dimensional matrices
        # VALUE :

        if self.type == "UTIL":
            # UTIL messages are multi-dimensional matrices
            shape = self.content.shape
            size = 1
            for s in shape:
                size *= s
            return size

        elif self.type == "VALUE":
            # VALUE message are a value assignment for each var in the
            # separator of the sender
            return len(self.content[0]) * 2

    def __str__(self):
        return "DpopMessage({}, {})".format(self._msg_type, self._content)


class DpopAlgo(VariableComputation):
    """
    DPOP: Dynamic Programming Optimization Protocol

    When running this algorithm, the DFS tree must be already defined and the
    children, parents and pseudo-parents must be known.

    In DPOP:
    * A computation represents, and select a value for, one variable.
    * A constraint is managed (i.e. referenced) by a single computation object:
      this means that, when building the computations, each constraint must only be
      passed as argument to a single computation.
    * A constraint must always be managed by the lowest node in the DFS
      tree that the relation depends on (which is especially important for
      non-binary relation). The pseudo-tree building mechanism already
      takes care of this.


    DPOP computations support two kinds of messages:
    * UTIL message:
      sent from children to parent, contains a relation (as a
      multi-dimensional matrix) with one dimension for each variable in our
      separator.
    * VALUE messages :
      contains the value of the parent of the node and the values of all
      variables that were present in our UTIl message to our parent (that is
      to say, our separator) .


    Parameters
    ----------
     variable: Variable
        The Variable object managed by this algorithm

     parent: variable name (str)
        the parent for this node. A node has at most one parent
        but may have 0-n pseudo-parents. Pseudo parent are not given
        explicitly but can be deduced from the constraints and children
        (if the union of the constraints' scopes contains a variable that is not a
        children, it must necessarily be a pseudo-parent).
        If the variable shares a constraints with its parent (which is the
        most common case), it must be present in the relation arg.

     children: name of children variables (list of str)
        the children variables of the variable argument, in the DFS tree

     constraints: List of Constraints
        constraints managed by this computation. These
        relations will be used when calculating costs. It must
        depends on the variable arg. Unary relation are also supported.
        Remember that a relation must always be managed by the lowest node in
        the DFS tree that the relation depends on (which is especially
        important for non-binary relation).

    comp_def: ComputationDef
        computation definition, gives the algorithm name (must be dpop) and the mode
        (min or max)
    """

    def __init__(
        self,
        variable: Variable,
        parent: str,
        children: Iterable[str],
        constraints: Iterable[Constraint],
        comp_def=None,
    ):

        super().__init__(variable, comp_def)

        assert comp_def.algo.algo == "dpop"

        self._mode = comp_def.algo.mode
        self._parent = parent
        self._children = list(children)
        self._constraints = constraints

        if hasattr(self._variable, "cost_for_val"):
            costs = []
            for d in self._variable.domain:
                costs.append(self._variable.cost_for_val(d))
            self._joined_utils = NAryMatrixRelation(
                [self._variable], costs, name="joined_utils"
            )

        else:
            self._joined_utils = NAryMatrixRelation([], name="joined_utils")

        self._children_separator = {}

        self._waited_children = []
        if not self.is_leaf:
            # If we are not a leaf, we must wait for the util messages from
            # our children.
            # This must be done in __init__ and not in on_start because we
            # may get an util message from one of our children before
            # running on_start, if this child computation start faster of
            # before us
            self._waited_children = list(self._children)

    def footprint(self):
        return computation_memory(self.computation_def.node)

    @property
    def is_root(self):
        return self._parent is None

    @property
    def is_leaf(self):
        return len(self._children) == 0

    def on_start(self):
        msg_count, msg_size = 0, 0

        if self.is_leaf and not self.is_root:
            # If we are a leaf in the DFS Tree we can immediately compute
            # our util and send it to our parent.
            # Note: as a leaf, our separator is the union of our parents and
            # pseudo-parents
            util = self._compute_utils_msg()
            self.logger.info(
                "Leaf %s init message %s -> %s  : %s",
                self._variable.name,
                self._variable.name,
                self._parent,
                util,
            )
            msg = DpopMessage("UTIL", util)
            self.post_msg(self._parent, msg)
            msg_count += 1
            msg_size += msg.size

        elif self.is_leaf:
            # we are both root and leaf : means we are a isolated variable we
            #  can select our own value alone:
            if self._constraints:
                for r in self._constraints:
                    self._joined_utils = join_utils(self._joined_utils, r)

                values, current_cost = find_arg_optimal(
                    self._variable, self._joined_utils, self._mode
                )

                self.select_value_and_finish(values[0], float(current_cost))
            else:
                # If the variable is not constrained, we can simply take a value at
                # random:
                value = choice(self._variable.domain)
                self.select_value_and_finish(value, 0.0)

    def stop_condition(self):
        # dpop stop condition is easy at it only selects one single value !
        if self.current_value is not None:
            return ALGO_STOP
        else:
            return ALGO_CONTINUE

    def select_value_and_finish(self, value, cost):
        """
        Select a value for this variable.

        DPOP is not iterative, once we have selected our value the algorithm
        is finished for this computation.

        Parameters
        ----------
        value: any (depends on the domain)
            the selected value
        cost: float
            the local cost for this value

        """

        self.value_selection(value, cost)
        self.stop()
        self.finished()
        self.logger.info("Value selected at %s : %s - %s", self.name, value, cost)

    @register("UTIL")
    def _on_util_message(self, variable_name, recv_msg, t) -> None:
        """
        Message handler for UTIL messages.

        Parameters
        ----------
        variable_name: str
            name of the variable that sent the message
        recv_msg: DpopMessage
            received message
        t: int
            message timestamp

        """
        self.logger.debug("Util message from %s : %r ", variable_name, recv_msg.content)
        utils = recv_msg.content
        msg_count, msg_size = 0, 0

        # accumulate util messages until we got the UTIL from all our children
        self._joined_utils = join_utils(self._joined_utils, utils)
        try:
            self._waited_children.remove(variable_name)
        except ValueError as e:
            self.logger.error(
                "Unexpected UTIL message from %s on %s : %r ",
                variable_name,
                self.name,
                recv_msg,
            )
            raise e
        # keep a reference of the separator of this children, we need it when
        # computing the value message
        self._children_separator[variable_name] = utils.dimensions

        if len(self._waited_children) == 0:

            if self.is_root:
                # We are the root of the DFS tree and have received all utils
                # we can select our own value and start the VALUE phase.

                # The root obviously has no parent nor pseudo parent, yet it
                # may have unary relations (with it-self!)
                for r in self._constraints:
                    self._joined_utils = join_utils(self._joined_utils, r)

                values, current_cost = find_arg_optimal(
                    self._variable, self._joined_utils, self._mode
                )
                selected_value = values[0]

                self.logger.info(
                    "ROOT: On UNTIL message from %s, send value "
                    "msg to childrens %s ",
                    variable_name,
                    self._children,
                )
                for c in self._children:
                    msg = DpopMessage("VALUE", ([self._variable], [selected_value]))
                    self.post_msg(c, msg)
                    msg_count += 1
                    msg_size += msg.size

                self.select_value_and_finish(selected_value, float(current_cost))
            else:
                # We have received the Utils msg from all our children, we can
                # now compute our own utils relation by joining the accumulated
                # util with the relations with our parent and pseudo_parents.
                util = self._compute_utils_msg()
                msg = DpopMessage("UTIL", util)
                self.logger.info(
                    "On UTIL message from %s, send UTILS msg " "to parent %s ",
                    variable_name,
                    self._children,
                )
                self.post_msg(self._parent, msg)
                msg_count += 1
                msg_size += msg.size

    def _compute_utils_msg(self):

        for r in self._constraints:
            self._joined_utils = join_utils(self._joined_utils, r)

        # use projection to eliminate self out of the message to our parent
        util = projection(self._joined_utils, self._variable, self._mode)

        return util

    @register("VALUE")
    def _on_value_message(self, variable_name, recv_msg, t) -> None:
        """
        Message handler for VALUE messages.

        Parameters
        ----------
        variable_name: str
            name of the variable that sent the message
        recv_msg: DpopMessage
            received message
        t: int
            message timestamp
        """
        self.logger.debug(
            '{}: on value message from {} : "{}"'.format(
                self.name, variable_name, recv_msg
            )
        )

        value = recv_msg.content
        msg_count, msg_size = 0, 0

        # Value msg contains the optimal assignment for all variables in our
        # separator : sep_vars, sep_values = value
        value_dict = {k.name: v for k, v in zip(*value)}
        self.logger.debug("Slicing relation on %s", value_dict)

        # as the value msg contains values for all variables in our
        # separator, slicing the util on these variables produces a relation
        # with a single dimension, our own variable.
        rel = self._joined_utils.slice(value_dict)

        self.logger.debug("Relation after slicing %s", rel)

        values, current_cost = find_arg_optimal(self._variable, rel, self._mode)
        selected_value = values[0]

        for c in self._children:
            variables_msg = [self._variable]
            values_msg = [selected_value]

            # own_separator intersection child_separator union
            # self.current_value
            for v in self._children_separator[c]:
                try:
                    values_msg.append(value_dict[v.name])
                    variables_msg.append(v)
                except KeyError:
                    # we want an intersection, we can ignore the variable if
                    # not in value_dict
                    pass
            msg = DpopMessage("VALUE", (variables_msg, values_msg))
            msg_count += 1
            msg_size += msg.size
            self.post_msg(c, msg)

        self.select_value_and_finish(selected_value, float(current_cost))
