import sys; sys.path.insert(0,'/tmp')
from xref import *
import capstone, collections, json
md=capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_32); md.detail=False
def off_of(va): return t0+(va-tv)
def cstr(va):
    for s in pe.sections:
        a=base+s.VirtualAddress
        if a<=va<a+s.SizeOfRawData:
            o=s.PointerToRawData+va-a; e=data.index(b'\0',o); return data[o:e].decode('latin1')
SET=0x9adbc0; NEW=0x9adab0
# find all call sites to SET and NEW
def callsites(target):
    out=[]
    for m in re.finditer(b'\xe8',text):
        p=m.start()
        if p+5>len(text): continue
        rel=struct.unpack('<i',text[p+1:p+5])[0]
        if tv+p+5+rel==target: out.append(tv+p)
    return out
sets=callsites(SET); news=callsites(NEW)
print(len(sets),'SetMember calls',len(news),'NewNative calls')
rows=[]
for c in sets:
    ins=list(md.disasm(data[off_of(c)-60:off_of(c)],c-60))
    ins=[i for i in ins if i.address<c][-12:]
    name=None; handler=None
    for i in reversed(ins):
        if i.mnemonic=='push' and i.op_str.startswith('0x'):
            v=int(i.op_str,16)
            if name is None and 0xb70000<=v<0xbd0000:
                s=cstr(v)
                if s and s.isidentifier(): name=s
    # handler: nearest NEW call before c, its preceding push imm in text
    nc=[n for n in news if c-80<n<c]
    if nc:
        n=nc[-1]; ii=list(md.disasm(data[off_of(n)-12:off_of(n)],n-12))
        for i in reversed([i for i in ii if i.address<n]):
            if i.mnemonic=='push' and i.op_str.startswith('0x'):
                v=int(i.op_str,16)
                if tv<=v<tv+tn: handler=v; break
    rows.append((c,fstart(off_of(c)),name,handler))
json.dump([(hex(a),hex(b),n,hex(h) if h else None) for a,b,n,h in rows],open('/tmp/regs2.json','w'))
print('rows',len(rows),'named',sum(1 for r in rows if r[2]),'with handler',sum(1 for r in rows if r[3]))
