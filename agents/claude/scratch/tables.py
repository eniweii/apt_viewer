import sys,struct,collections,json
sys.argv=['x']
sys.path.insert(0,'/home/claude/apt_viewer/Apt/beamng')
import engine_map as E
from capstone import x86
exe=E.Exe('/tmp/stuff/nfs.exe')
rows=E.registrations(exe)
dsec=[(exe.base+s.VirtualAddress, exe.base+s.VirtualAddress+s.SizeOfRawData) for s in exe.pe.sections[1:3]]
indata=lambda v: any(a<=v<b for a,b in dsec)
def rd(va,n):
    for s in exe.pe.sections:
        a=exe.base+s.VirtualAddress
        if a<=va<a+s.SizeOfRawData: o=s.PointerToRawData+va-a; return exe.data[o:o+n]
    return b'\0'*n
cache={}
def scan(f):
    if f in cache: return cache[f]
    movs=[];cmps=[];adds=[];calls=set()
    for i in exe.insns(f,exe.func_end(f)):
        ops=i.op_str.split(', ')
        if i.mnemonic=='call' and i.operands and i.operands[0].type==x86.X86_OP_IMM:
            v=i.operands[0].imm&0xffffffff
            if exe.tv<=v<exe.tv+exe.tn: calls.add(v)
        if len(ops)==2 and ops[1].startswith('0x'):
            v=int(ops[1],16)
            if i.mnemonic=='mov' and indata(v): movs.append((ops[0],v))
            if i.mnemonic=='cmp' and indata(v): cmps.append((ops[0],v))
            if i.mnemonic=='add' and v<=0x100: adds.append((ops[0],v))
    cache[f]=(movs,cmps,adds,calls); return cache[f]
def find_tables():
    tables={}
    for r in rows:
        h=r['handler']
        if not h: continue
        seen={h}; fr=[h]
        for _ in range(3):
            nxt=[]
            for f in fr:
                movs,cmps,adds,calls=scan(f)
                for reg,s in movs:
                    for reg2,e in cmps:
                        if reg2==reg and 0<e-s<0x40000:
                            for reg3,st in adds:
                                if reg3==reg and st>=4 and (e-s)%st==0:
                                    tables.setdefault((s,e,st),set()).add((str(r['member']),hex(f)))
                for c in calls-seen:
                    if len(seen)<200: seen.add(c); nxt.append(c)
            fr=nxt
    return tables
def word(v):
    if v==0: return '0'
    if exe.tv<=v<exe.tv+exe.tn: return 'code:%x'%v
    s=exe.cstr(v) if v>=0x400000 else None
    if s: return repr(s)
    if indata(v): return 'data:%x'%v
    return str(v) if v<100000 else hex(v)
if __name__=='__main__':
    T=find_tables()
    for (s,e,st),users in sorted(T.items()):
        print('== %x..%x stride %d (%d entries) used by %s'%(s,e,st,(e-s)//st,sorted({u[0] for u in users})[:6]))
        for k in range(min(4,(e-s)//st)):
            ws=struct.unpack('<%dI'%(st//4),rd(s+k*st,st)); print('   ',[word(w) for w in ws])
