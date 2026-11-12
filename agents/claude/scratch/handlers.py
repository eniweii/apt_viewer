import sys; sys.path.insert(0,'/tmp'); sys.path.insert(0,'/tmp/part2/stuffpart2')
from xref import *
from lang import lookup2
import capstone, json, collections
from capstone import x86
md=capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_32); md.detail=True
M=0xffffffff
def bsh(s):
    x=0xFFFFFFFF
    for c in s: x=(x*33+c)&M
    return x
labels=[l.rstrip('\n') for l in open('/tmp/part2/stuffpart2/hashes_main.txt',encoding='latin1') if l.strip() and not l.startswith('#')]
H={}
for l in labels:
    b=l.encode('latin1')
    for nm,fn in (('bsh',bsh),('vlt',lambda s:lookup2(s,0xFFFFFFFF)),('bin',lambda s:lookup2(s,0xABCDEF00))):
        H.setdefault(fn(b),(nm,l))
        if nm=='vlt': H.setdefault(fn(l.lower().encode()),('vlt',l))
import pickle; pickle.dump(H,open('/tmp/H.pkl','wb')); print(len(H),'hash entries')
