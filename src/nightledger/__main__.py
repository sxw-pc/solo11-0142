import argparse,json
from pathlib import Path
from . import slot,calibration,exposure,photometry,report
from .store import connect,read

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['slot','calibration','exposure','photometry','report','demo'])
    p.add_argument('--db',default='nightledger.sqlite');p.add_argument('--input');a=p.parse_args()
    db=connect(a.db)
    try:
        if a.command=='report':out=report.build(db)
        elif a.command=='demo':
            root=Path.cwd()/'fixtures';runs=[]
            for command in ('slot','calibration','exposure'):
                with db:runs.append(globals()[command].run(db,read(root/f'{command}-ok.json')))
            out={'runs':runs,'report':report.build(db)}
        else:
            if not a.input:p.error('--input required')
            with db:out=globals()[a.command].run(db,read(a.input))
        print(json.dumps({'module_file':__file__,'result':out},ensure_ascii=False,indent=2))
    finally:db.close()
if __name__=='__main__':main()
