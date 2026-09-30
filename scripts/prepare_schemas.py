#!/usr/bin/env python3
"""Regenerate public schema resources and per-scheme codes from pinned Rime sources."""
import pathlib,json,re,shutil,subprocess,hashlib
root=pathlib.Path(__file__).resolve().parents[1]
import argparse
parser=argparse.ArgumentParser()
parser.add_argument('upstreams',type=pathlib.Path,help='Directory containing the five official Rime checkouts pinned in SOURCES.json')
base=parser.parse_args().upstreams
expected=json.loads((root/'third-party/rime-schemes/SOURCES.json').read_text())
for item in expected:
 folder=base/item['repository'].rsplit('/',1)[1]
 assert subprocess.check_output(['git','-C',str(folder),'rev-parse','HEAD'],text=True).strip()==item['commit'], 'Unexpected upstream revision'
out=root/'input-method/data/voicefly'
out.mkdir(parents=True,exist_ok=True)
licenses=root/'third-party/rime-schemes';licenses.mkdir(parents=True,exist_ok=True)
manifest=[]
for repo in sorted(base.iterdir()):
 if not repo.is_dir():continue
 commit=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
 files={}
 for source in repo.glob('*.yaml'):
  shutil.copy2(source,out/source.name)
  files[source.name]=hashlib.sha256(source.read_bytes()).hexdigest()
 for name in ['LICENSE','AUTHORS','README.md']:
  if (repo/name).is_file():
   destination=licenses/repo.name;destination.mkdir(exist_ok=True)
   shutil.copy2(repo/name,destination/name)
 manifest.append({'repository':'https://github.com/rime/'+repo.name,'commit':commit,'files':files})
(root/'third-party/rime-schemes/SOURCES.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2,sort_keys=True)+'\n')
schemes={'pinyin':'pinyin_simp','flypy':'double_pinyin_flypy','natural':'double_pinyin','microsoft':'double_pinyin_mspy','wubi':'wubi86','wubiPinyin':'wubi_pinyin'}
for name,origin in schemes.items():
 text=(out/(origin+'.schema.yaml')).read_text()
 text=re.sub(r'(schema_id:)\s*'+origin+r'\b',r'\1 voicefly_'+name,text,count=1)
 text=re.sub(r'(?m)^(  name:).+$',r'\1 "Voice Fly · '+{'pinyin':'全拼','flypy':'小鹤双拼','natural':'自然码双拼','microsoft':'微软双拼','wubi':'五笔86','wubiPinyin':'五笔拼音'}[name]+'"',text,count=1)
 (out/('voicefly_'+name+'.schema.yaml')).write_text('# Voice Fly schema wrapper; upstream source and license in third-party/rime-schemes.\n'+text)
readings={};weights={};syllables=set();all_readings={}
for line in (out/'pinyin_simp.dict.yaml').read_text().splitlines():
 parts=line.split('\t')
 if len(parts)<2 or line.startswith('#'):continue
 text,py=parts[:2]
 if not re.fullmatch(r'[a-zv ]+',py):continue
 syllables.update(py.split())
 if py not in all_readings.setdefault(text,[]):all_readings[text].append(py)
 try:weight=float(parts[2].rstrip('%')) if len(parts)>2 else 0
 except ValueError:weight=0
 if text not in readings or weight>weights[text]:readings[text]=py;weights[text]=weight
codes={}
for name,origin in [('natural','double_pinyin'),('microsoft','double_pinyin_mspy')]:
 text=(out/(origin+'.schema.yaml')).read_text()
 section=text.split('  algebra:\n',1)[1].split('\ntranslator:',1)[0]
 rules=[line.strip()[2:] for line in section.splitlines() if line.strip().startswith('- ')]
 table={}
 for syllable in sorted(syllables):
  values={syllable}
  for rule in rules:
   kind,pattern,replacement,*rest=rule.split('/')
   if kind in ['xform','derive']:
    replacement=re.sub(r'\$(\d)',r'\\g<\1>',replacement)
    transformed={re.sub(pattern,replacement,x) for x in values}
    values=values|transformed if kind=='derive' else transformed
   elif kind=='xlit':values={x.translate(str.maketrans(pattern,replacement)) for x in values}
   elif kind=='erase':values={x for x in values if not re.search(pattern,x)}
  accepted=sorted(x for x in values if re.fullmatch('[a-z;]{2}',x))
  if accepted:table[syllable]=accepted[0]
 codes[name]=table
wubi={}
for line in (out/'wubi86.dict.yaml').read_text().splitlines():
 parts=line.split('\t')
 if len(parts)>=2 and re.fullmatch('[a-z]{1,4}',parts[1]):
  text,code=parts[:2]
  if text not in wubi or len(code)>len(wubi[text]):wubi[text]=code
codes['wubi']=wubi
(out/'word-codes.json').write_text(json.dumps({'codes':codes,'readings':readings,'all_readings':all_readings},ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n')
print('Prepared',len(readings),'readings,',len(wubi),'Wubi entries; natural/microsoft',len(codes['natural']),len(codes['microsoft']))
