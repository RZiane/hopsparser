import os
import os.path
import sys

def make_csv_summary(dirname,goldfile): 

    header  = ''
    csv_out = open(dirname+'_summary.csv','w')  
    for filename in os.listdir(dirname):
        KVlist = filename[:-6].split('-')[1:]
        if not header:
            header = [KV.split(':')[0] for KV in KVlist]+['UAS','LAS']
            print(','.join(header),file=csv_out)
        values = [KV.split(':')[1] for KV in KVlist]

        filename = os.path.join(dirname,filename)
        os.system('perl eval07.pl -q -g %s -s %s > tmp/eval.tmp'%(goldfile,filename))
        (las,uas) = process_eval07(filename)
        values.append(uas)
        values.append(las)
        print(','.join(values),file=csv_out)
    csv_out.close()
        
def process_eval07(filename):
    
    istream = open(filename)
    las = istream.readline()
    uas = istream.readline()
    istream.close()
    
    las = las.split('=')[1]
    las = float(las[:-2])
    uas = uas.split('=')[1]
    uas = float(uas[:-2])
    return (las,uas)

make_csv_summary(sys.argv[1],'../spmrl/test.French.pred.conll')
