# -*- coding: utf-8 -*-

# Keypatch IDA Plugin, powered by Keystone Engine (www.keytone-engine.org).
# By Nguyen Anh Quynh & Thanh Nguyen, 2016.

# Keypatch is released under the GPL v2. See COPYING for more information.
# Find docs & latest version at http://www.keystone-engine.org/keypatch

# This IDA plugin includes 2 tools inside: Patcher & Assembler.
# Access to both tools via menu Edit | Keypatch
# Hotkey to activate Keypatch Patcher is CTRL+ALT+K

import idc
import idaapi
import re
from collections import OrderedDict
from keystone import *


VERSION = "1.0"


MAX_INSTRUCTION_STRLEN = 64
MAX_ENCODING_LEN = 40
MAX_ADDRESS_LEN = 40
ENCODING_ERR_OUTPUT = "..."

def to_hexstr(buf, sep=' '):
    return sep.join("{:02x}".format(ord(c)) for c in buf).upper()

## Main Keypatch class
class Keypatch_Asm:

    # supported architectures
    arch_lists = {
        "X86 16-bit": (KS_ARCH_X86, KS_MODE_16),                # X86 16-bit
        "X86 32-bit": (KS_ARCH_X86, KS_MODE_32),                # X86 32-bit
        "X86 64-bit": (KS_ARCH_X86, KS_MODE_64),                # X86 64-bit
        "ARM": (KS_ARCH_ARM, KS_MODE_ARM),                      # ARM
        "ARM Thumb": (KS_ARCH_ARM, KS_MODE_THUMB),              # ARM Thumb
        "ARM64 (ARMV8)": (KS_ARCH_ARM64, KS_MODE_LITTLE_ENDIAN),# ARM64
        "Hexagon": (KS_ARCH_HEXAGON, KS_MODE_BIG_ENDIAN),       # Hexagon
        "Mips32": (KS_ARCH_MIPS, KS_MODE_MIPS32),               # Mips32
        "Mips64": (KS_ARCH_MIPS, KS_MODE_MIPS64),               # Mips64
        "PowerPC 32": (KS_ARCH_PPC, KS_MODE_PPC32),             # PPC32
        "PowerPC 64": (KS_ARCH_PPC, KS_MODE_PPC64),             # PPC64
        "Sparc 32": (KS_ARCH_SPARC, KS_MODE_SPARC32),           # Sparc32
        "Sparc 64": (KS_ARCH_SPARC, KS_MODE_SPARC64),           # Sparc64
        "SystemZ": (KS_ARCH_SYSTEMZ, KS_MODE_BIG_ENDIAN),       # SystemZ
    }

    endian_lists = {
        "Little Endian": KS_MODE_LITTLE_ENDIAN,                 # little endian
        "Big Endian": KS_MODE_BIG_ENDIAN,                       # big endian
    }

    def __init__(self, arch=None, mode=None):

        # sort architecture list by name
        self.arch_lists = OrderedDict(sorted(self.arch_lists.items(), key=lambda x:x[0], reverse=False))

        # we do not want syntaxes in random order
        self.syntax_lists = OrderedDict()
        self.syntax_lists["Intel"] = KS_OPT_SYNTAX_INTEL
        self.syntax_lists["Nasm"] = KS_OPT_SYNTAX_NASM
        self.syntax_lists["AT&T"] = KS_OPT_SYNTAX_ATT

        # update current arch and mode
        self.update_hardware_mode()

        # override arch & mode if provided
        if arch is not None:
            self.arch = arch
        if mode is not None:
            self.mode = mode

        # IDA uses Intel syntax by default
        self.syntax = KS_OPT_SYNTAX_INTEL

    # return Keystone arch & mode (with endianess included)
    @staticmethod
    def get_hardware_mode():

        (arch, mode) = (None, None)

        # heuristically detect hardware setup
        info = idaapi.get_inf_structure()
        cpuname = info.procName.lower()
        if cpuname == "metapc":
            arch = KS_ARCH_X86
            if info.is_64bit():
                mode = KS_MODE_64
            elif info.is_32bit():
                mode = KS_MODE_32
            else:
                mode = KS_MODE_16
        elif cpuname.startswith("arm"):
            # ARM or ARM64
            if info.is_64bit():
                arch = KS_ARCH_ARM64
                mode = KS_MODE_LITTLE_ENDIAN
            elif info.is_32bit():
                arch = KS_ARCH_ARM
                # either big-endian or little-endian
                if cpuname == "arm":
                    mode = KS_MODE_ARM | KS_MODE_LITTLE_ENDIAN
                else:
                    mode = KS_MODE_ARM | KS_MODE_BIG_ENDIAN
        elif cpuname.startswith("sparc"):
            arch = KS_ARCH_SPARC
            if info.is_64bit():
                mode = KS_MODE_SPARC64
            else:
                mode = KS_MODE_SPARC32
            if cpuname == "sparcb":
                mode += KS_MODE_BIG_ENDIAN
            else:
                mode += KS_MODE_LITTLE_ENDIAN
        elif cpuname.startswith("ppc"):
            arch = KS_ARCH_PPC
            if info.is_64bit():
                mode = KS_MODE_PPC64
            else:
                mode = KS_MODE_PPC32
            if cpuname == "ppc":
                # do not support Little Endian mode for PPC
                mode += KS_MODE_BIG_ENDIAN
        elif cpuname.startswith("mips"):
            arch = KS_ARCH_MIPS
            if info.is_64bit():
                mode = KS_MODE_MIPS64
            else:
                mode = KS_MODE_MIPS32
            if cpuname == "mipsl":
                mode += KS_MODE_LITTLE_ENDIAN
            else:
                mode += KS_MODE_BIG_ENDIAN
        elif cpuname.startswith("systemz") or cpuname.startswith("s390x"):
            arch = KS_ARCH_SYSTEMZ
            mode = KS_MODE_BIG_ENDIAN

        return (arch, mode)

    def update_hardware_mode(self):
        (self.arch, self.mode) = self.get_hardware_mode()

    # normalize assembly code
    # remove comment at the end of assembly code
    @staticmethod
    def asm_normalize(text):
        text = ' '.join(text.split())
        if text.rfind(';') != -1:
            return text[:text.rfind(';')].strip()

        return text.strip()

    @staticmethod
    # check if input address is valid
    # return
    #       -1  invalid address at target binary
    #        0  type mismatch of input address
    #        1  valid address at target binary
    def check_address(address):
        try:
            if idaapi.isEnabled(address):
                return 1
            else:
                return -1
        except:
            # invalid type
            return 0

    ### resolve IDA names from input asm code
    # todo: a better syntax parser for all archs
    def ida_resolve(self, assembly, address=idc.BADADDR):
        def _resolve(_op, ignore_kw=True):
            names = re.findall(r"\b[a-z0-9_:\.]+\b", _op, re.I)

            # try to resolve all names
            for name in names:
                # ignore known keywords
                if ignore_kw and name in ('byte', 'near', 'short', 'word', 'dword', 'ptr', 'offset'):
                    continue

                sym = name

                # split segment reg
                parts = name.partition(':')
                if parts[2] != '':
                    sym = parts[2]

                (t, v) = idaapi.get_name_value(address, sym)

                # skip if name doesn't exist or segment / segment registers
                if t in (idaapi.NT_SEG, idaapi.NT_NONE):
                    continue

                _op = _op.replace(sym, '0x{:X}'.format(v))

            return _op

        if self.check_address(address) == 0:
            print("Keypatch: WARNING: invalid input address {}".format(address))
            return assembly

        # for now, we only support IDA name resolve for X86, ARM, ARM64, MIPS, PPC, SPARC
        if not (self.arch in (KS_ARCH_X86, KS_ARCH_ARM, KS_ARCH_ARM64, KS_ARCH_MIPS, KS_ARCH_PPC, KS_ARCH_SPARC)):
            return assembly

        _asm = assembly.partition(' ')
        mnem = _asm[0]
        opers = _asm[2].split(',')

        for idx, op in enumerate(opers):
            _op = list(op.partition('['))
            ignore_kw = True
            if _op[1] == '':
                _op[2] = _op[0]
                _op[0] = ''
            else:
                _op[0] = _resolve(_op[0], ignore_kw=True)
                ignore_kw = False

            _op[2] = _resolve(_op[2], ignore_kw=ignore_kw)

            opers[idx] = ''.join(_op)

        asm = "{} {}".format(mnem, ','.join(opers))
        return asm

    # return bytes of instruction or data
    # return None on failure
    def ida_get_item(self, address, hex_output=False):

        if self.check_address(address) != 1:
            # not a valid address
            return (None, 0)

        # return None if address is in the middle of instruction / data
        if address != idc.ItemHead(address):
            return (None, 0)

        len = idc.ItemSize(address)
        item = idaapi.get_many_bytes(address, len)

        if item is None:
            return (None, 0)

        if hex_output:
            item = to_hexstr(item)

        return (item, len)

    @staticmethod
    def get_op_dtype_name(op_idx):
        dtyp_lists = {
            idaapi.dt_byte: 'byte',     #  8 bit
            idaapi.dt_word: 'word',     #  16 bit
            idaapi.dt_dword: 'dword',   #  32 bit
            idaapi.dt_float: 'dword',   #  4 byte
            idaapi.dt_double: 'dword',  #  8 byte
            #idaapi.dt_tbyte = 5        #  variable size (ph.tbyte_size)
            #idaapi.dt_packreal = 6         #  packed real format for mc68040
            idaapi.dt_qword: 'qword',   #  64 bit
            idaapi.dt_byte16: 'xmmword',#  128 bit
            #idaapi.dt_code = 9         #  ptr to code (not used?)
            #idaapi.dt_void = 10        #  none
            #idaapi.dt_fword = 11       #  48 bit
            #idaapi.dt_bitfild = 12     #  bit field (mc680x0)
            #idaapi.dt_string = 13      #  pointer to asciiz string
            #idaapi.dt_unicode = 14     #  pointer to unicode string
            #idaapi.dt_3byte = 15       #  3-byte data
            #idaapi.dt_ldbl = 16        #  long double (which may be different from tbyte)
            idaapi.dt_byte32: 'ymmword',# 256 bit
        }

        dtype = idaapi.cmd.Operands[op_idx].dtyp
        dtyp_size = idaapi.get_dtyp_size(dtype)
        if dtype == idaapi.dt_tbyte:
            if dtyp_size == 10:
                return 'xword'

        dtyp_name = dtyp_lists.get(idaapi.cmd.Operands[op_idx].dtyp, None)

        return dtyp_name

    # get disasm from IDA
    # return '' on invalid address
    def ida_get_disasm(self, address, fixup=False):

        def GetMnem(asm):
            sp = asm.find(' ')
            if (sp == -1):
                return asm
            return asm[:sp]

        if self.check_address(address) != 1:
            # not a valid address
            return ''

        # return if address is in the middle of instruction / data
        if address != idc.ItemHead(address):
            return ''

        asm = self.asm_normalize(idc.GetDisasm(address))
        # for now, only support IDA syntax fixup for Intel CPU
        if not fixup or self.arch != KS_ARCH_X86:
            return asm

        # KS_ARCH_X86 mode
        # rebuild disasm code from IDA
        i = 0
        mnem = GetMnem(asm)
        if mnem == '' or mnem in ('rep', 'repne', 'repe'):
            return asm

        opers = []
        while GetOpType(address, i) > 0 and i < 6:
            t = GetOpType(address, i)
            o = GetOpnd(address, i)

            if t in (idc.o_mem, o_displ):
                parts = list(o.partition(':'))
                if parts[2] == '':
                    parts[2] = parts[0]
                    parts[0] = ''

                if '[' not in parts[2]:
                    parts[2] = '[{}]'.format(parts[2])

                o = ''.join(parts)

                if 'ptr ' not in o:
                    dtyp_name = self.get_op_dtype_name(i)
                    if dtyp_name != None:
                        o = "{} ptr {}".format(dtyp_name, o)

            opers.append(o)
            i += 1

        asm = mnem
        for o in opers:
            if o != '':
                asm = "{} {},".format(asm, o)

        asm = asm.strip(',')
        return asm

    # assemble code with Keystone
    # return (encoding, count), or (None, 0) on failure
    def assemble(self, assembly, address, arch=None, mode=None, syntax=None):

        # return assembly with arithmetic equation evaluated
        def eval_operand(assembly, start, stop, prefix=''):
            imm = assembly[start+1:stop]
            try:
                eval_imm = eval(imm)
                if eval_imm > 0x80000000:
                    eval_imm = 0xffffffff - eval_imm
                    eval_imm += 1
                    eval_imm = -eval_imm
                return assembly.replace(prefix + imm, prefix + hex(eval_imm))
            except:
                return assembly

        # IDA uses different syntax from Keystone
        # sometimes, we can convert code to be consumable by Keystone
        def fix_ida_syntax(assembly):

            # return True if this insn needs to be fixed
            def check_arm_arm64_insn(arch, mnem):
                if arch == KS_ARCH_ARM:
                    if mnem.startswith("ldr") or mnem.startswith("str"):
                        return True
                    return False
                elif arch == KS_ARCH_ARM64:
                    if mnem.startswith("ldr") or mnem.startswith("str"):
                        return True
                    return mnem in ("stp")
                return False

            # return True if this insn needs to be fixed
            def check_ppc_insn(mnem):
                return mnem in ("stw")

            # replace the right most string occured
            def rreplace(s, old, new):
                li = s.rsplit(old, 1)
                return new.join(li)

            # convert some ARM pre-UAL assembly to UAL, so Keystone can handle it
            # example: streqb --> strbeq
            def fix_arm_ual(mnem, assembly):
                # TODO: this is not an exhaustive list yet
                if len(mnem) != 6:
                    return assembly

                if (mnem[-1] in ('s', 'b', 'h', 'd')):
                    #print(">> 222", mnem[3:5])
                    if mnem[3:5] in ("cc", "eq", "ne", "hs", "lo", "mi", "pl", "vs", "vc", "hi", "ls", "ge", "lt", "gt", "le", "al"):
                        return assembly.replace(mnem, mnem[:3] + mnem[-1] + mnem[3:5], 1)

                return assembly

            if self.arch != KS_ARCH_X86:
                assembly = assembly.lower()
            else:
                # Keystone does not support immediate 0bh, but only 0Bh
                assembly = assembly.upper()

            # however, 0X must be converted to 0x
            # Keystone should fix this limitation in the future
            assembly = assembly.replace("0X", " 0x")

            _asm = assembly.partition(' ')
            mnem = _asm[0]
            if mnem == '':
                return assembly

            #print(">> asm =", _asm)
            #print(">> mnem =", mnem)

            # for PPC, Keystone does not accept registers with 'r' prefix,
            # but only the number behind. lets try to fix that here by
            # removing the prefix 'r'.
            if self.arch == KS_ARCH_PPC:
                #print(">> PPC asm =", assembly)
                for n in range(32):
                    r = " r%u," %n
                    if r in assembly:
                        assembly = assembly.replace(r, " %u," %n)
                for n in range(32):
                    r = "(r%u)" %n
                    if r in assembly:
                        assembly = assembly.replace(r, "(%u)" %n)
                for n in range(32):
                    r = ", r%u" %n
                    if assembly.endswith(r):
                        assembly = rreplace(assembly, r, ", %u" %n)

            if self.arch == KS_ARCH_X86:
                if mnem == "RETN":
                    # replace retn with ret
                    return assembly.replace('RETN', 'RET', 1)
                if 'OFFSET ' in assembly:
                    return assembly.replace('OFFSET ', ' ')
                if mnem in ('CALL', 'JMP') or mnem.startswith('LOOP'):
                    # remove 'NEAR PTR'
                    if ' NEAR PTR ' in assembly:
                        return assembly.replace(' NEAR PTR ', ' ')
                elif mnem[0] == 'J':
                    # JMP instruction
                    if ' SHORT ' in assembly:
                        # remove ' short '
                        return assembly.replace(' SHORT ', ' ')
            elif self.arch in (KS_ARCH_ARM, KS_ARCH_ARM64, KS_ARCH_PPC):
                # *** ARM
                # LDR     R1, [SP+rtld_fini],#4
                # STR     R2, [SP,#-4+rtld_fini]!
                # STR     R0, [SP,#fini]!
                # STR     R12, [SP,#4+var_8]!

                # *** ARM64
                # STP     X29, X30, [SP,#-0x10+var_150]!
                # STR     W0, [X29,#0x150+var_8]
                # LDR     X0, [X0,#(qword_4D6678 - 0x4D6660)]
                # TODO:
                # ADRP    X19, #interactive@PAGE

                # *** PPC
                # stw     r5, 0x120+var_108(r1)

                if self.arch == KS_ARCH_ARM:
                    #print(">> before UAL fix: ", assembly)
                    assembly = fix_arm_ual(mnem, assembly)
                    #print(">> after UAL fix: ", assembly)

                if check_arm_arm64_insn(self.arch, mnem) or (("[" in assembly) and ("]" in assembly)):
                    bang = assembly.find('#')
                    bracket = assembly.find(']')
                    if bang != -1 and bracket != -1 and bang < bracket:
                        return eval_operand(assembly, bang, bracket, '#')
                    elif '+0x0]' in assembly:
                        return assembly.replace('+0x0]', ']')
                elif check_ppc_insn(mnem):
                    start = assembly.find(', ')
                    stop = assembly.find('(')
                    if start != -1 and stop != -1 and start < stop:
                        return eval_operand(assembly, start, stop)
            return assembly

        def is_thumb(address):
            return idc.GetReg(address, 'T') == 1

        if self.check_address(address) == 0:
            return (None, 0)

        if arch == KS_ARCH_ARM and is_thumb(address):
            mode = KS_MODE_THUMB

        # use default syntax, arch and mode if not provided
        if syntax is None:
            syntax = self.syntax
        if arch is None:
            arch = self.arch
        if mode is None:
            mode = self.mode

        try:
            ks = Ks(arch, mode)
            if arch == KS_ARCH_X86:
                ks.syntax = syntax
            encoding, count = ks.asm(fix_ida_syntax(assembly), address)
        except KsError as e:
            # keep the below code for debugging
            #print("Keypatch Err: {}".format(e))
            #print("Original asm: {}".format(assembly))
            #print("Fixed up asm: {}".format(fix_ida_syntax(assembly)))
            encoding, count = None, 0

        return (encoding, count)

    # return number of bytes patched
    # return
    #    0  Invalid assembly
    #   -1  PatchByte failure
    #   -2  Can't read original data
    #   -3  Invalid address
    def patch_code(self, address, assembly, syntax, padding=False):

        # patch at address, return the number of written bytes
        def _patch(address, patch_data, len):
            ea = address
            orig_data = ''
            invalid_value = False

            while ea < (address+len):
                if not invalid_value:
                    orig_byte = idc.Byte(ea)

                    if not idc.hasValue(idc.GetFlags(ea)):
                        print("Keypatch: WARNING: 0x{:X} has no defined value. ".format(ea))
                        invalid_value = True
                    else:
                        orig_data += chr(orig_byte)

                patch_byte = ord(patch_data[ea - address])
                if patch_byte != orig_byte:
                    # patch one byte
                    if idaapi.patch_byte(ea, patch_byte) != 1:
                        print("Keypatch: FAILED to patch byte at 0x{:X} [0x{:X}]".format(ea, patch_byte))
                        break
                ea += 1
            return (ea-address, orig_data)

        if self.check_address(address) != 1:
            # not a valid address
            return -3

        (orig_encoding, orig_len) = self.ida_get_item(address)
        if (orig_encoding, orig_len) == (None, 0):
            return -2

        (encoding, count) = self.assemble(assembly, address, syntax=syntax)
        if encoding is None:
            return 0

        patch_len = len(encoding)
        encoding = ''.join(chr(c) for c in encoding)

        if encoding == orig_encoding:
            print("Keypatch: no need to patch, same encoding data [{}] at 0x{:X}".format(to_hexstr(orig_encoding), address))
            return orig_len

        if padding and patch_len < orig_len:
            # for now, only support NOP padding on Intel CPU
            if self.arch == KS_ARCH_X86:
                nop = "\x90"
                patch_len = orig_len
                encoding = encoding.ljust(patch_len, nop)

        (plen, p_orig_data) = _patch(address, encoding, patch_len)
        if plen != patch_len:
            # patch failure

            if plen > 0:
                # revert the changes
                (rlen, _) = _patch(address, p_orig_data, plen)
                if rlen == plen:
                    print("Keypatch: successfully reverted changes of {:d} byte(s) at 0x{:X} [{}]".format(
                                        plen, address, to_hexstr(p_orig_data)))
                else:
                    print("Keypatch: FAILED to revert changes of {:d} byte(s) at 0x{:X} [{}]".format(
                                        plen, address, to_hexstr(p_orig_data)))

            return -1

        print("Keypatch: successfully patched {:d} byte(s) at 0x{:X} from [{}] to [{}]".format(plen,
                                        address, to_hexstr(p_orig_data), to_hexstr(encoding)))

        return plen


    ### Form helper functions
    @staticmethod
    def get_value_by_idx(dictionary, idx, default=None):
        try:
            items = dictionary.values()
            val = items[idx]
        except IndexError:
            val = default

        return val

    @staticmethod
    def find_idx_by_value(dictionary, value, default=None):
        try:
            items = dictionary.values()
            idx = items.index(value)
        except:
            idx = default

        return idx

    def get_arch_by_idx(self, idx):
        return self.get_value_by_idx(self.arch_lists, idx)

    def find_arch_idx(self, arch, mode):
        return self.find_idx_by_value(self.arch_lists, (arch, mode))

    def get_syntax_by_idx(self, idx):
        return self.get_value_by_idx(self.syntax_lists, idx, self.syntax)

    def find_syntax_idx(self, syntax):
        return self.find_idx_by_value(self.syntax_lists, syntax)
    ### /Form helper functions


# Dialog for interactive assembler & patcher
# Common ancestor form to be shared between Patcher & Assmembler
class Keypatch_Form(idaapi.Form):
    # prepare for form initializing
    def setup(self, kp_asm, address, assembly=None):

        self.kp_asm = kp_asm
        self.address = address

        # update current arch & mode
        self.kp_asm.update_hardware_mode()

        # find right value for c_arch & c_endian controls
        mode = self.kp_asm.mode
        self.endian_id = 0   # little endian
        if self.kp_asm.mode & KS_MODE_BIG_ENDIAN:
            self.endian_id = 1   # big endian
            mode = self.kp_asm.mode - KS_MODE_BIG_ENDIAN

        self.arch_id = self.kp_asm.find_arch_idx(self.kp_asm.arch, mode)

        self.syntax_id = 0  # to make non-X86 arch happy
        if self.kp_asm.arch == KS_ARCH_X86:
            self.syntax_id = self.kp_asm.find_syntax_idx(self.kp_asm.syntax)

        # get original instruction and bytes
        self.orig_asm = kp_asm.ida_get_disasm(address)
        (self.orig_encoding, self.orig_len) = kp_asm.ida_get_item(address, hex_output=True)
        if self.orig_encoding == None:
            self.orig_encoding = ''

        if assembly is None:
            self.asm = self.kp_asm.ida_get_disasm(self.address, fixup=True)
        else:
            self.asm = assembly


    def __init__(self, kp_asm, address, assembly=None, patch_mode=False, opts=0):
        pass

    # update Encoding control
    # return True on success, False on failure
    def _update_encoding(self, arch, mode):
        try:
            syntax = None
            if arch == KS_ARCH_X86:
                syntax_id = self.GetControlValue(self.c_syntax)
                syntax = self.kp_asm.get_syntax_by_idx(syntax_id)

            address = self.GetControlValue(self.c_addr)
            try:
                idaapi.isEnabled(address)
            except:
                # invalid address value
                address = 0

            assembly = self.GetControlValue(self.c_assembly)
            raw_assembly = self.kp_asm.ida_resolve(assembly, address)
            self.SetControlValue(self.c_raw_assembly, raw_assembly)

            (encoding, count) =  self.kp_asm.assemble(raw_assembly, address, arch=arch,
                                                    mode=mode, syntax=syntax)

            if encoding is None:
                self.SetControlValue(self.c_encoding, ENCODING_ERR_OUTPUT)
                return False
            else:
                text = ""
                for byte in encoding:
                    text += "%02X " % byte
                text.strip()
                if text == "":
                    # error?
                    self.SetControlValue(self.c_encoding, ENCODING_ERR_OUTPUT)
                    return False
                else:
                    self.SetControlValue(self.c_encoding, text.strip())
                    self.SetControlValue(self.c_encoding_len, len(encoding))
                    return True
        except Exception,e:
            print (str(e))
            import traceback
            traceback.print_exc()
            self.SetControlValue(self.c_encoding, ENCODING_ERR_OUTPUT)
            return False

    # callback to be executed when any form control changed
    def OnFormChange(self, fid):
        return 1

    # update some controls - including Encoding control
    def update_controls(self, arch, mode):
        # Fixup & Encoding-len are read-only controls
        self.EnableField(self.c_raw_assembly, False)
        self.EnableField(self.c_encoding_len, False)

        # Encoding is enable to allow user to select & copy
        self.EnableField(self.c_encoding, True)

        if self.GetControlValue(self.c_endian) == 1:
            endian = KS_MODE_BIG_ENDIAN
        else:
            endian = KS_MODE_LITTLE_ENDIAN

        # update encoding with live assembling
        self._update_encoding(arch, mode | endian)

        return 1


# Patcher form
class Keypatch_Patcher(Keypatch_Form):
    def __init__(self, kp_asm, address, assembly=None, opts=0):
        self.setup(kp_asm, address, assembly)

        # create Patcher form
        Form.__init__(self,
            r"""STARTITEM {id:c_assembly}
BUTTON YES* Patch
KEYPATCH:: Patcher

            {FormChangeCb}
            <Endian     :{c_endian}>
            <Syntax     :{c_syntax}>
            <Address    :{c_addr}>
            <Original   :{c_orig_assembly}>
             <-   Encode:{c_orig_encoding}>
             <-   Size  :{c_orig_len}>
            <Assembly   :{c_assembly}>
             <-   Fixup :{c_raw_assembly}>
             <-   Encode:{c_encoding}>
             <-   Size  :{c_encoding_len}>
            <Padding extra bytes with NOPs:{c_opt_padding}>{c_opt_chk}>
            """, {
            'c_endian': Form.DropdownListControl(
                          items = self.kp_asm.endian_lists.keys(),
                          readonly = True,
                          selval = self.endian_id),
            'c_addr': Form.NumericInput(value=address, swidth=MAX_ADDRESS_LEN, tp=Form.FT_ADDR),
            'c_assembly': Form.StringInput(value=self.asm[:MAX_INSTRUCTION_STRLEN], width=MAX_INSTRUCTION_STRLEN),
            'c_orig_assembly': Form.StringInput(value=self.orig_asm[:MAX_INSTRUCTION_STRLEN], width=MAX_INSTRUCTION_STRLEN),
            'c_orig_encoding': Form.StringInput(value=self.orig_encoding[:MAX_ENCODING_LEN], width=MAX_ENCODING_LEN),
            'c_orig_len': Form.NumericInput(value=self.orig_len, swidth=8, tp=Form.FT_DEC),
            'c_raw_assembly': Form.StringInput(value='', width=MAX_INSTRUCTION_STRLEN),
            'c_encoding': Form.StringInput(value='', width=MAX_ENCODING_LEN),
            'c_encoding_len': Form.NumericInput(value=0, swidth=8, tp=Form.FT_DEC),
            'c_syntax': Form.DropdownListControl(
                          items = self.kp_asm.syntax_lists.keys(),
                          readonly = True,
                          selval = self.syntax_id),
            'c_opt_chk':idaapi.Form.ChkGroupControl(('c_opt_padding', ''), value=opts),
            'FormChangeCb': Form.FormChangeCb(self.OnFormChange),
            })

        self.Compile()

    # get Patcher options
    def get_opts(self):
        names = self.c_opt_chk.children_names
        val = self.c_opt_chk.value
        opts = {}
        for i in range(len(names)):
            opts[names[i]] = val & (2**i)
        return opts

    # callback to be executed when any form control changed
    def OnFormChange(self, fid):
        (arch, mode) = (self.kp_asm.arch, self.kp_asm.mode)

        # make address, arch, endian and syntax readonly in patch_mode mode
        self.EnableField(self.c_orig_assembly, False)
        self.EnableField(self.c_orig_encoding, False)
        self.EnableField(self.c_orig_len, False)

        self.EnableField(self.c_endian, False)
        self.EnableField(self.c_addr, False)

        if arch == KS_ARCH_X86:
            # do not show Endian control
            self.ShowField(self.c_endian, False)
            # allow to choose Syntax
            self.ShowField(self.c_syntax, True)
        else:   # do not show Syntax control for non-X86 mode
            self.ShowField(self.c_syntax, False)
            # for now, we do not support padding for non-X86 archs
            self.ShowField(self.c_opt_chk, False)
            #self.EnableField(self.c_opt_padding, False)
            #self.c_opt_padding.checked = False

        # update other controls & Encoding with live assembling
        self.update_controls(arch, mode)

        return 1


# Assembler form
class Keypatch_Assembler(Keypatch_Form):
    def __init__(self, kp_asm, address, assembly=None):
        self.setup(kp_asm, address, assembly)

        # create Assembler form
        Form.__init__(self,
            r"""STARTITEM {id:c_assembly}
BUTTON YES* Close
KEYPATCH:: Assembler

            {FormChangeCb}
            <Arch       :{c_arch}>
            <Endian     :{c_endian}>
            <Syntax     :{c_syntax}>
            <Address    :{c_addr}>
            <Assembly   :{c_assembly}>
             <-   Fixup :{c_raw_assembly}>
             <-   Encode:{c_encoding}>
             <-   Size  :{c_encoding_len}>
            """, {
            'c_addr': Form.NumericInput(value=address, swidth=MAX_ADDRESS_LEN, tp=Form.FT_ADDR),
            'c_assembly': Form.StringInput(value=self.asm[:MAX_INSTRUCTION_STRLEN], width=MAX_INSTRUCTION_STRLEN),
            'c_raw_assembly': Form.StringInput(value='', width=MAX_INSTRUCTION_STRLEN),
            'c_encoding': Form.StringInput(value='', width=MAX_ENCODING_LEN),
            'c_encoding_len': Form.NumericInput(value=0, swidth=8, tp=Form.FT_DEC),
            'c_arch': Form.DropdownListControl(
                          items = self.kp_asm.arch_lists.keys(),
                          readonly = True,
                          selval = self.arch_id,
                          width = 32),
            'c_endian': Form.DropdownListControl(
                          items = self.kp_asm.endian_lists.keys(),
                          readonly = True,
                          selval = self.endian_id),
            'c_syntax': Form.DropdownListControl(
                          items = self.kp_asm.syntax_lists.keys(),
                          readonly = True,
                          selval = self.syntax_id),
            'FormChangeCb': Form.FormChangeCb(self.OnFormChange),
            })

        self.Compile()

    # callback to be executed when any form control changed
    def OnFormChange(self, fid):
        # only Assembler mode allows to select arch+mode
        arch_id = self.GetControlValue(self.c_arch)
        (arch, mode) = self.kp_asm.get_arch_by_idx(arch_id)

        if arch == KS_ARCH_X86:
            # enable Syntax and disable Endian for x86
            self.ShowField(self.c_syntax, True)
            self.EnableField(self.c_syntax, True)
            self.syntax_id = self.GetControlValue(self.c_syntax)
            self.EnableField(self.c_endian, False)
            # set Endian index properly
            self.SetControlValue(self.c_endian, 0)
        elif arch in (KS_ARCH_ARM64, KS_ARCH_HEXAGON, KS_ARCH_SYSTEMZ):
            # no Syntax & Endian option for these archs
            self.ShowField(self.c_syntax, False)
            self.EnableField(self.c_syntax, False)
            self.EnableField(self.c_endian, False)
            # set Endian index properly
            self.SetControlValue(self.c_endian, (mode & KS_MODE_BIG_ENDIAN != 0))
        elif (arch == KS_ARCH_PPC) and (mode & KS_MODE_PPC32 != 0):
            # no Syntax & Endian option for these archs
            self.ShowField(self.c_syntax, False)
            self.EnableField(self.c_syntax, False)
            self.EnableField(self.c_endian, False)
            # set Endian index properly
            self.SetControlValue(self.c_endian, (mode & KS_MODE_BIG_ENDIAN != 0))
        else:
            # no Syntax & Endian option
            self.ShowField(self.c_syntax, False)
            self.EnableField(self.c_syntax, False)
            self.EnableField(self.c_endian, True)

        if self.GetControlValue(self.c_endian) == 1:
            endian = KS_MODE_BIG_ENDIAN
        else:
            endian = KS_MODE_LITTLE_ENDIAN

        # update other controls & Encoding with live assembling
        self.update_controls(arch, mode)

        return 1


#--------------------------------------------------------------------------
# Plugin
#--------------------------------------------------------------------------
class Keypatch_Plugin_t(idaapi.plugin_t):
    comment = "Keypatch plugin for IDA Pro (using Keystone framework)"
    help = "Find more information on Keypatch at http://keystone-engine.org/keypatch"
    wanted_name = "Keypatch patcher (CTRL+ALT+K)"
    wanted_hotkey = ""
    flags = idaapi.PLUGIN_KEEP

    def init(self):
        # add a menu for Keypatch patcher & assembler
        menu_ctx = idaapi.add_menu_item("Edit/Keypatch/", "Patcher", "Ctrl-Alt-K", 1, self.patcher, None)
        if menu_ctx is not None:
            idaapi.add_menu_item("Edit/Keypatch/", "Assembler", "", 1, self.assembler, None)
            print("=" * 80)
            print("Keypatch registered IDA plugin {} (c) Nguyen Anh Quynh & Thanh Nguyen, 2016".format(VERSION))
            print("Keypatch is using Keystone v{}".format(keystone.__version__))
            print("Keypatch Patcher's shortcut key is CTRL+ALT+K")
            print("Keypatch Assembler is available from menu Edit | Keypatch | Assembler")
            print("Find more information about Keypatch at http://keystone-engine.org/keypatch")
            print("=" * 80)

            self.kp_asm = Keypatch_Asm()

        return idaapi.PLUGIN_KEEP

    def term(self):
        pass

    # handler for Assembler menu
    def assembler(self):
        address = idc.ScreenEA()
        f = Keypatch_Assembler(self.kp_asm, address)
        f.Execute()
        f.Free()

    # handler for Patcher menu
    def patcher(self):
        # be sure that this arch is supported by Keystone
        if self.kp_asm.arch is None:
            Warning("ERROR: this architecture is unsupported by Keystone, quit!")
            return

        address = idc.ScreenEA()
        # turn on padding by default
        init_opts = 1
        init_assembly = None
        while True:
            f = Keypatch_Patcher(self.kp_asm, address, assembly=init_assembly, opts=init_opts)
            ok = f.Execute()
            if ok == 1:
                try:
                    syntax = None
                    if f.kp_asm.arch == KS_ARCH_X86:
                        syntax_id = f.c_syntax.value
                        syntax = self.kp_asm.get_syntax_by_idx(syntax_id)

                    assembly = f.c_assembly.value
                    opts = f.get_opts()
                    padding = (opts.get("c_opt_padding", 0) != 0)

                    raw_assembly = self.kp_asm.ida_resolve(assembly, address)

                    print("Keypatch: attempt to modify \"{}\" at 0x{:X} to \"{}\"".format(
                            self.kp_asm.ida_get_disasm(address), address, assembly))

                    length = self.kp_asm.patch_code(address, raw_assembly, syntax, padding=padding)

                    if length > 0:
                        # update start address pointing to the next instruction
                        init_assembly = None
                        address += length
                    else:
                        init_assembly = f.c_assembly.value
                        if length == 0:
                            Warning("ERROR: invalid assembly [{}]".format(assembly))
                        elif length == -1:
                            Warning("ERROR: failed to patch binary at 0x{:X}!".format(address))
                        elif length == -2:
                            Warning("ERROR: can't read original data at 0x{:X}, try again".format(address))


                except KsError as e:
                    print("Keypatch Err: {}".format(e))
            else:   # Cancel
                f.Free()
                break
            f.Free()


    def run(self, arg):
        self.patcher()


# register IDA plugin
def PLUGIN_ENTRY():
    return Keypatch_Plugin_t()
