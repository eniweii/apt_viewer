import sys, pickle; sys.path.insert(0,'/tmp')
from xref import *
import capstone, json, collections
from capstone import x86
H=pickle.load(open('/tmp/H.pkl','rb'))
md=capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_32); md.detail=True
def cstr(va):
    for s in pe.sections:
        a=base+s.VirtualAddress
        if a<=va<a+s.SizeOfRawData:
            o=s.PointerToRawData+va-a; e=data.find(b'\0',o,o+80)
            if e<0: return None
            b=data[o:e]
            if len(b)>=3 and all(32<=c<127 for c in b): return b.decode()
    return None
def analyze(h):
    o=t0+(h-tv); end=data.find(b'\xcc\xcc',o); end=min(end if end>0 else o+4000,o+6000)
    strs=[];hashes=[];calls=[]
    for i in md.disasm(data[o:end],h):
        for op in i.operands:
            if op.type==x86.X86_OP_IMM:
                v=op.imm&0xffffffff
                if i.mnemonic=='call': calls.append(v); continue
                if v>=0x400000:
                    s=cstr(v)
                    if s: strs.append(s)
                if v in H: hashes.append(H[v][1])
    return strs,hashes,calls,(end-o)
