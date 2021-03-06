import marshal
from collections import namedtuple

from . import dis
from .token import Token


# Get all the opcodes into globals
globals().update(dis.opmap)


class Scanner:

    def run(self, bytecode):
        code_object = marshal.loads(bytecode)
        tokens = self.tokenize(code_object)
        return tokens

    def tokenize(self, co):
        """
        Convert code object <co> into a sequence of tokens.

        Based on dis.disassemble() function.
        """
        # Container for tokens
        tokens = []
        self.code = code = co.co_code
        codelen = len(code)
        self.build_lines_data(co)
        self.build_prev_op()
        self.find_new_ifs()
        # Get jump targets
        # Format: {target offset: [jump offset, ...]}
        jump_targets = self.find_jump_targets()
        # Initialize extended arg at 0. When extended arg op is encountered,
        # variable preserved for next cycle and added as arg for next op
        extended_arg = 0
        free = None
        for offset in self.op_range(0, codelen):
            # Process new ifs
            if offset in self.new_ifs.values():
                # Create fake tonken, which is needed by parser
                token = Token()
                token.type = dis.opname[JUMP_FORWARD]
                token.offset = '{}_fake'.format(offset)
                token.linestart = False
                token.attr = 0
                token.pattr = repr(offset)
                tokens.append(token)
                # Add info to jump targets as well
                jumps = jump_targets.setdefault(offset, [])
                jumps.append(offset)
            # Add jump target tokens
            if offset in jump_targets:
                jump_idx = 0
                for jump_offset in jump_targets[offset]:
                    tokens.append(Token('COME_FROM', None, repr(jump_offset),
                                        offset='{}_{}'.format(offset, jump_idx)))
                    jump_idx += 1
            op = code[offset]
            # Create token and fill all the fields we can
            # w/o touching arguments
            current_token = Token()
            current_token.type = dis.opname[op]
            current_token.offset = offset
            current_token.linestart = True if offset in self.linestarts else False
            if op >= dis.HAVE_ARGUMENT:
                # Calculate op's argument value based on its argument and
                # preceding extended argument, if any
                oparg = code[offset+1] + code[offset+2]*256 + extended_arg
                extended_arg = 0
                if op == dis.EXTENDED_ARG:
                    extended_arg = oparg*65536

                # Fill token's attr/pattr fields
                current_token.attr = oparg
                if op in dis.hasconst:
                    current_token.pattr = repr(co.co_consts[oparg])
                elif op in dis.hasname:
                    current_token.pattr = co.co_names[oparg]
                elif op in dis.hasjrel:
                    current_token.pattr = repr(offset + 3 + oparg)
                elif op in dis.haslocal:
                    current_token.pattr = co.co_varnames[oparg]
                elif op in dis.hascompare:
                    current_token.pattr = dis.cmp_op[oparg]
                elif op in dis.hasfree:
                    if free is None:
                        free = co.co_cellvars + co.co_freevars
                    current_token.pattr = free[oparg]
            tokens.append(current_token)
        return tokens

    def build_lines_data(self, code_obj):
        """
        Generate various line-related helper data.
        """
        # Offset: lineno pairs, only for offsets which start line.
        # Locally we use list for more convenient iteration using indices
        # Format: [(offset, line started), ...]
        linestarts = list(dis.findlinestarts(code_obj))
        # Format: {offset: line started}
        self.linestarts = dict(linestarts)
        # Plain set with offsets of first ops on line
        # Format: set(offset, ...)
        self.linestart_offsets = {a for (a, _) in linestarts}
        # 'List-map' which shows line number of current op and offset of
        # first op on following line, given offset of op as index
        # Format (for each token offset): [(current line, next line begins offset)]
        self.lines = lines = []
        LineTuple = namedtuple('LineTuple', ['l_no', 'next'])
        # Iterate through available linestarts, and fill
        # the data for all code offsets encountered until
        # last linestart offset
        _, prev_line_no = linestarts[0]
        offset = 0
        for start_offset, line_no in linestarts[1:]:
            while offset < start_offset:
                lines.append(LineTuple(prev_line_no, start_offset))
                offset += 1
            prev_line_no = line_no
        # Fill remaining offsets with reference to last line number
        # and code length as start offset of following non-existing line
        codelen = len(self.code)
        while offset < codelen:
            lines.append(LineTuple(prev_line_no, codelen))
            offset += 1

    def build_prev_op(self):
        """
        Compose 'list-map' which allows to jump to previous
        op, given offset of current op as index.
        """
        code = self.code
        codelen = len(code)
        # Format: (for each offset) [previous token offset, ...]
        self.prev_op = [0]
        for offset in self.op_range(0, codelen):
            op = code[offset]
            for _ in range(self.op_size(op)):
                self.prev_op.append(offset)

    def op_size(self, op):
        """
        Return size of operator with its arguments
        for given opcode <op>.
        """
        if op < dis.HAVE_ARGUMENT:
            return 1
        else:
            return 3

    def op_range(self, start, end):
        """
        Iterate through positions of opcodes, skipping
        arguments.
        """
        while start < end:
            yield start
            start += self.op_size(self.code[start])

    def find_new_ifs(self):
        # Format: {jump op offset: jump target}
        self.new_ifs = {}
        code = self.code
        for offset in self.op_range(0, len(code)):
            op = code[offset]
            if op in (POP_JUMP_IF_FALSE, POP_JUMP_IF_FALSE, POP_JUMP_IF_TRUE, POP_JUMP_IF_TRUE):
                target = self.get_target(offset)
                src_line = self.lines[offset][0]
                tgt_line = self.lines[target][0]
                # Means and/or logic
                if src_line == tgt_line:
                    continue
                stop = False
                for inner_offset in self.op_range(offset, target):
                    inner_op = code[inner_offset]
                    # If-else constructs contain jump forward in-between,
                    # and it also jumps across the lines
                    if inner_op == JUMP_FORWARD:
                        inner_target = self.get_target(inner_offset)
                        inner_src_line = self.lines[inner_offset][0]
                        inner_tgt_line = self.lines[inner_target][0]
                        if inner_tgt_line != inner_src_line:
                            stop = True
                            break
                    # While constructs jump back at the end of the cycle
                    elif inner_op == JUMP_ABSOLUTE:
                        inner_target = self.get_target(inner_offset)
                        if inner_target < offset:
                            stop = True
                            break
                if stop:
                    continue
                self.new_ifs[offset] = target

    def find_jump_targets(self):
        """
        Detect all offsets in a byte code which are jump targets.

        Return the map between targets and sources.
        """
        code = self.code
        codelen = len(code)
        self.structs = [{'type':  'root',
                         'start': 0,
                         'end':   codelen-1}]

        # Map fixed jumps to their real destination
        self.fixed_jumps = {}
        self.build_statement_indices()
        # Containers filled by detect_structure()
        self.not_continue = set()
        self.return_end_ifs = set()

        targets = {}
        for offset in self.op_range(0, codelen):
            op = code[offset]

            # Determine structures and fix jumps
            self.detect_structure(offset)

            if op >= dis.HAVE_ARGUMENT:
                label = self.fixed_jumps.get(offset)
                oparg = code[offset+1] + code[offset+2] * 256

                if label is None:
                    if op in dis.hasjrel and op != FOR_ITER:
                        label = offset + 3 + oparg
                    elif op in dis.hasjabs:
                        if op in (JUMP_IF_FALSE_OR_POP, JUMP_IF_TRUE_OR_POP):
                            if oparg > offset:
                                label = oparg

                if label is not None and label != -1:
                    targets[label] = targets.get(label, []) + [offset]
            elif op == END_FINALLY and offset in self.fixed_jumps:
                label = self.fixed_jumps[offset]
                targets[label] = targets.get(label, []) + [offset]
        return targets


    def build_statement_indices(self):
        code = self.code
        start = 0
        end = codelen = len(code)

        statement_opcodes = {
            SETUP_LOOP, BREAK_LOOP, CONTINUE_LOOP,
            SETUP_FINALLY, END_FINALLY, SETUP_EXCEPT, SETUP_WITH,
            POP_BLOCK, STORE_FAST, DELETE_FAST, STORE_DEREF,
            STORE_GLOBAL, DELETE_GLOBAL, STORE_NAME, DELETE_NAME,
            STORE_ATTR, DELETE_ATTR, STORE_SUBSCR, DELETE_SUBSCR,
            RETURN_VALUE, RAISE_VARARGS, POP_TOP, PRINT_EXPR,
            JUMP_ABSOLUTE
        }

        statement_opcode_sequences = [(POP_JUMP_IF_FALSE, JUMP_FORWARD), (POP_JUMP_IF_FALSE, JUMP_ABSOLUTE),
                                      (POP_JUMP_IF_TRUE, JUMP_FORWARD), (POP_JUMP_IF_TRUE, JUMP_ABSOLUTE)]

        designator_ops = {
            STORE_FAST, STORE_NAME, STORE_GLOBAL, STORE_DEREF, STORE_ATTR,
            STORE_SUBSCR, UNPACK_SEQUENCE, JUMP_ABSOLUTE
        }

        # "Simple" and "compound" statements
        # Format: [offset, ...]
        prelim = self.all_instr(start, end, statement_opcodes)

        # Initialize final container with statements with
        # preliminnary data. Contains offsets of stmt opcodes
        # and stmt opcode sequences
        stmts = self.stmts = set(prelim)
        # "Compound" statements
        # Format: [offset, ...]
        pass_stmts = set()


        # Find stmt opcode sequences
        for sequence in statement_opcode_sequences:
            for i in self.op_range(start, end-(len(sequence)+1)):
                match = True
                for elem in sequence:
                    if elem != code[i]:
                        match = False
                        break
                    i += self.op_size(code[i])

                if match is True:
                    i = self.prev_op[i]
                    stmts.add(i)
                    pass_stmts.add(i)

        # Initialize statement list with the full data we've gathered so far
        # For now it's copy of stmts - both simple and compound statement offsets
        # are written in here
        if pass_stmts:
            stmt_offset_list = list(stmts)
            stmt_offset_list.sort()
        else:
            stmt_offset_list = prelim

        # 'List-map' which contains offset of start of
        # next statement, when op offset is passed as index
        self.next_stmt = []
        last_stmt_offset = -1
        i = 0
        # Go through all statement offsets
        for stmt_offset in stmt_offset_list:
            # Process absolute jumps, but do not remove 'pass' statements
            # from the set
            if code[stmt_offset] == JUMP_ABSOLUTE and stmt_offset not in pass_stmts:
                # If absolute jump occurs in forward direction or it takes off from the
                # same line as previous statement, this is not a statement
                target = self.get_target(stmt_offset)
                if target > stmt_offset or self.lines[last_stmt_offset].l_no == self.lines[stmt_offset].l_no:
                    stmts.remove(stmt_offset)
                    continue
                # Rewind ops till we encounter non-JA one
                j = self.prev_op[stmt_offset]
                while code[j] == JUMP_ABSOLUTE:
                    j = self.prev_op[j]
                # If we got here, then it's list comprehension which
                # is not a statement too
                if code[j] == LIST_APPEND:
                    stmts.remove(stmt_offset)
                    continue
            # Exclude ROT_TWO + POP_TOP
            elif code[stmt_offset] == POP_TOP and code[self.prev_op[stmt_offset]] == ROT_TWO:
                stmts.remove(stmt_offset)
                continue
            # Exclude FOR_ITER + designators
            elif code[stmt_offset] in designator_ops:
                j = self.prev_op[stmt_offset]
                while code[j] in designator_ops:
                    j = self.prev_op[j]
                if code[j] == FOR_ITER:
                    stmts.remove(stmt_offset)
                    continue
            # Add to list another list with offset of current statement,
            # equal to length of previous statement
            self.next_stmt += [stmt_offset] * (stmt_offset-i)
            last_stmt_offset = stmt_offset
            i = stmt_offset
        # Finish filling the list for last statement
        self.next_stmt += [codelen] * (codelen-len(self.next_stmt))


    def all_instr(self, start, end, instr, target=None, include_beyond_target=False):
        """
        Find all <instr> in the block from start to end.
        <instr> is any python bytecode instruction or a list of opcodes
        If <instr> is an opcode with a target (like a jump), a target
        destination can be specified which must match precisely.

        Return a list with indexes to them or [] if none found.
        """
        code = self.code
        assert(start >= 0 and end <= len(code))

        try:
            None in instr
        except:
            instr = [instr]

        result = []
        for offset in self.op_range(start, end):
            op = code[offset]
            if op in instr:
                if target is None:
                    result.append(offset)
                else:
                    t = self.get_target(offset)
                    if include_beyond_target and t >= target:
                        result.append(offset)
                    elif t == target:
                        result.append(offset)
        return result

    def last_instr(self, start, end, instr, target=None, exact=True):
        """
        Find the last <instr> in the block from start to end.
        <instr> is any python bytecode instruction or a list of opcodes
        If <instr> is an opcode with a target (like a jump), a target
        destination can be specified which must match precisely if exact
        is True, or if exact is False, the instruction which has a target
        closest to <target> will be returned.

        Return index to it or None if not found.
        """

        code = self.code
        # Make sure requested positions do not go out of
        # code bounds
        if not (start >= 0 and end <= len(code)):
            return None

        try:
            None in instr
        except:
            instr = [instr]

        result_offset = None
        current_distance = len(code)
        for offset in self.op_range(start, end):
            op = code[offset]
            if op in instr:
                if target is None:
                    result_offset = offset
                else:
                    dest = self.get_target(offset)
                    if dest == target:
                        current_distance = 0
                        result_offset = offset
                    elif not exact:
                        new_distance = abs(target - dest)
                        if new_distance <= current_distance:
                            current_distance = new_distance
                            result_offset = offset
        return result_offset

    def get_target(self, offset):
        """
        Get target offset for op located at given <offset>.
        """
        op = self.code[offset]
        target = self.code[offset+1] + self.code[offset+2] * 256
        if op in dis.hasjrel:
            target += offset + 3
        return target

    def detect_structure(self, offset):
        """
        Detect structures and their boundaries to fix optimizied jumps
        """
        code = self.code
        op = code[offset]
        # Detect parent structure
        parent = self.structs[0]
        start = parent['start']
        end = parent['end']

        # Pick inner-most parent for our offset
        for struct in self.structs:
            curent_start = struct['start']
            curent_end   = struct['end']
            if (curent_start <= offset < curent_end) and (curent_start >= start and curent_end <= end):
                start = curent_start
                end = curent_end
                parent = struct

        if op in (POP_JUMP_IF_FALSE, POP_JUMP_IF_TRUE):
            start = offset + self.op_size(op)
            target = self.get_target(offset)
            rtarget = self.restrict_to_parent(target, parent)
            prev_op = self.prev_op

            # Do not let jump to go out of parent struct bounds
            if target != rtarget and parent['type'] == 'and/or':
                self.fixed_jumps[offset] = rtarget
                return

            # Does this jump to right after another cond jump?
            # If so, it's part of a larger conditional
            if (code[prev_op[target]] in (JUMP_IF_FALSE_OR_POP, JUMP_IF_TRUE_OR_POP,
                                          POP_JUMP_IF_FALSE, POP_JUMP_IF_TRUE)) and (target > offset):
                self.fixed_jumps[offset] = prev_op[target]
                self.structs.append({'type': 'and/or',
                                     'start': start,
                                     'end': prev_op[target]})
                return
            # Is it an and inside if block
            if op == POP_JUMP_IF_FALSE:
                # Search for other POP_JUMP_IF_FALSE targetting the same op,
                # in current statement, starting from current offset, and filter
                # everything inside inner 'or' jumps and midline ifs
                match = self.rem_or(start, self.next_stmt[offset], POP_JUMP_IF_FALSE, target)
                match = self.remove_mid_line_ifs(match)
                # If we still have any offsets in set, start working on it
                if match:
                    if (code[prev_op[rtarget]] in (JUMP_FORWARD, JUMP_ABSOLUTE) and prev_op[rtarget] not in self.stmts and
                        self.restrict_to_parent(self.get_target(prev_op[rtarget]), parent) == rtarget):
                        if (code[prev_op[prev_op[rtarget]]] == JUMP_ABSOLUTE and self.remove_mid_line_ifs([offset]) and
                            target == self.get_target(prev_op[prev_op[rtarget]]) and
                            (prev_op[prev_op[rtarget]] not in self.stmts or self.get_target(prev_op[prev_op[rtarget]]) > prev_op[prev_op[rtarget]]) and
                            1 == len(self.remove_mid_line_ifs(self.rem_or(start, prev_op[prev_op[rtarget]], (POP_JUMP_IF_FALSE, POP_JUMP_IF_TRUE), target)))):
                            pass
                        elif (code[prev_op[prev_op[rtarget]]] == RETURN_VALUE and self.remove_mid_line_ifs([offset]) and
                              1 == (len(set(self.remove_mid_line_ifs(self.rem_or(start, prev_op[prev_op[rtarget]],
                                                                                 (POP_JUMP_IF_FALSE, POP_JUMP_IF_TRUE), target))) |
                                    set(self.remove_mid_line_ifs(self.rem_or(start, prev_op[prev_op[rtarget]],
                                                                             (POP_JUMP_IF_FALSE, POP_JUMP_IF_TRUE, JUMP_ABSOLUTE),
                                                                             prev_op[rtarget], True)))))):
                            pass
                        else:
                            fix = None
                            jump_ifs = self.all_instr(start, self.next_stmt[offset], POP_JUMP_IF_FALSE)
                            last_jump_good = True
                            for j in jump_ifs:
                                if target == self.get_target(j):
                                    if self.lines[j].next == j + 3 and last_jump_good:
                                        fix = j
                                        break
                                else:
                                    last_jump_good = False
                            self.fixed_jumps[offset] = fix or match[-1]
                            return
                    else:
                        self.fixed_jumps[offset] = match[-1]
                        return
            # op == POP_JUMP_IF_TRUE
            else:
                next = self.next_stmt[offset]
                if prev_op[next] == offset:
                    pass
                elif code[next] in (JUMP_FORWARD, JUMP_ABSOLUTE) and target == self.get_target(next):
                    if code[prev_op[next]] == POP_JUMP_IF_FALSE:
                        if code[next] == JUMP_FORWARD or target != rtarget or code[prev_op[prev_op[rtarget]]] not in (JUMP_ABSOLUTE, RETURN_VALUE):
                            self.fixed_jumps[offset] = prev_op[next]
                            return
                elif (code[next] == JUMP_ABSOLUTE and code[target] in (JUMP_ABSOLUTE, JUMP_FORWARD) and
                      self.get_target(target) == self.get_target(next)):
                    self.fixed_jumps[offset] = prev_op[next]
                    return

            if (code[prev_op[rtarget]] == JUMP_ABSOLUTE and prev_op[rtarget] in self.stmts and
                prev_op[rtarget] != offset and prev_op[prev_op[rtarget]] != offset and
                not (code[rtarget] == JUMP_ABSOLUTE and code[rtarget+3] == POP_BLOCK and code[prev_op[prev_op[rtarget]]] != JUMP_ABSOLUTE)):
                rtarget = prev_op[rtarget]

            # Does the if jump just beyond a jump op, then this is probably an if statement
            if code[prev_op[rtarget]] in (JUMP_ABSOLUTE, JUMP_FORWARD):
                if_end = self.get_target(prev_op[rtarget])

                # Is this a loop not an if?
                if (if_end < prev_op[rtarget]) and (code[prev_op[if_end]] == SETUP_LOOP):
                    if(if_end > start):
                        return

                end = self.restrict_to_parent(if_end, parent)

                self.structs.append({'type': 'if-then',
                                     'start': start,
                                     'end': prev_op[rtarget]})
                self.not_continue.add(prev_op[rtarget])

                if rtarget < end:
                    self.structs.append({'type': 'if-else',
                                         'start': rtarget,
                                         'end': end})
            elif code[prev_op[rtarget]] == RETURN_VALUE:
                self.structs.append({'type': 'if-then',
                                     'start': start,
                                     'end': rtarget})
                self.return_end_ifs.add(prev_op[rtarget])

        elif op in (JUMP_IF_FALSE_OR_POP, JUMP_IF_TRUE_OR_POP):
            target = self.get_target(offset)
            if target > offset:
                unop_target = self.last_instr(offset, target, JUMP_FORWARD, target)
                if unop_target and code[unop_target+3] != ROT_TWO:
                    self.fixed_jumps[offset] = unop_target
                else:
                    self.fixed_jumps[offset] = self.restrict_to_parent(target, parent)

    def restrict_to_parent(self, target, parent):
        """Restrict target to parent structure boundaries."""
        if not (parent['start'] < target < parent['end']):
            target = parent['end']
        return target

    def rem_or(self, start, end, instr, target=None, include_beyond_target=False):
        """
        Find offsets of all requested <instr> between <start> and <end>,
        optionally <target>ing specified offset, and return list found
        <instr> offsets which are not within any POP_JUMP_IF_TRUE jumps.
        """
        # Find all offsets of requested instructions
        instr_offsets = self.all_instr(start, end, instr, target, include_beyond_target)
        # Get all POP_JUMP_IF_TRUE (or) offsets
        pjit_offsets = self.all_instr(start, end, POP_JUMP_IF_TRUE)
        filtered = []
        for pjit_offset in pjit_offsets:
            pjit_tgt = self.get_target(pjit_offset) - 3
            for instr_offset in instr_offsets:
                if instr_offset <= pjit_offset or instr_offset >= pjit_tgt:
                    filtered.append(instr_offset)
            instr_offsets = filtered
            filtered = []
        return instr_offsets

    def remove_mid_line_ifs(self, ifs):
        """
        Go through passed offsets, filtering ifs
        located somewhere mid-line.
        """
        filtered = []
        for if_ in ifs:
            # For each offset, if line number of current and next op
            # is the same
            if self.lines[if_].l_no == self.lines[if_+3].l_no:
                # Check if last op on line is PJIT or PJIF, and if it is - skip it
                if self.code[self.prev_op[self.lines[if_].next]] in (POP_JUMP_IF_TRUE, POP_JUMP_IF_FALSE):
                    continue
            filtered.append(if_)
        return filtered
