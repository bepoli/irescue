#!/usr/bin/env python

from irescue.misc import testGz
from irescue.misc import writerr
from irescue.misc import unGzip
from irescue.misc import run_shell_cmd
from irescue.misc import getlen
from pysam import idxstats, AlignmentFile, index
from gzip import open as gzopen
from os import makedirs
import sys, requests, io

# Check if bam file is indexed
def checkIndex(bamFile, verbose):
    with AlignmentFile(bamFile) as bam:
        if not bam.has_index():
            writerr('BAM index not found. Attempting to index the BAM...')
            try:
                index(bamFile)
            except:
                writerr(f'Couldn\'t index the BAM file. Please do so manually with `samtools index {bamFile}`.', error=True)
            else:
                writerr('BAM indexing done.')
        else:
            if verbose:
                writerr(f'Found index for BAM file {bamFile}', send=verbose)


# Check repeatmasker regions bed file format. Download if not provided.
# Returns the path of the repeatmasker bed file.
def makeRmsk(regions, genome, genomes, tmpdir, outname):
    # if a repeatmasker bed file is provided, use that
    if regions:
        if testGz(regions):
            f = gzopen(regions, 'rb')
            rl = lambda x: x.readline().decode()
        else:
            f = open(regions, 'r')
            rl = lambda x: x.readline()
        # skip header
        line = rl(f)
        while line[0] == '#':
            line = rl(f)
        # check for minimum column number
        if len(line.strip().split('\t')) < 4:
            writerr('Error: please provide a tab-separated BED file with at least 4 columns and TE feature name (e.g. subfamily) in 4th column.', error=True)
        f.close()
        out = regions
    # if no repeatmasker file is provided, and a genome assembly name is provided, download and prepare a rmsk.bed file
    elif genome:
        if not genome in genomes:
            writerr(
                f"ERROR: Genome assembly name shouldbe one of: {', '.join(genomes.keys())}",
                error=True
            )
        url, header_lines = genomes[genome]
        writerr(f'Downloading and parsing RepeatMasker annotation for assembly {genome} from {url} ...')
        try:
            response = requests.get(url, stream=True, timeout=60)
        except:
            writerr("ERROR: Download of RepeatMasker annotation failed. Couldn't connect to host.", error=True)
        rmsk = gzopen(io.BytesIO(response.content), 'rb')
        out = tmpdir + '/' + outname
        with open(out, 'w') as f:
            # print header
            h = ['#chr','start','end','name','score','strand']
            h = '\t'.join(h)
            h += '\n'
            f.write(h)
            # skip rmsk header
            for _ in range(header_lines):
                next(rmsk)
            # parse rmsk
            for line in rmsk:
                lst = line.decode('utf-8').strip().split()
                strand, subfamily, famclass = lst[8:11]
                if famclass.split('/')[0] in ['Low_complexity','Simple_repeat','rRNA','scRNA','srpRNA','tRNA']:
                    continue
                # concatenate family and class with subfamily
                subfamily += '~' + famclass
                score = lst[0]
                chr, start, end = lst[4:7]
                # make coordinates 0-based
                start = str(int(start)-1)
                if strand != '+':
                    strand = '-'
                outl = '\t'.join([chr, start, end, subfamily, score, strand])
                outl += '\n'
                f.write(outl)
    else:
        writerr('Error: it is mandatory to define either --regions OR --genome paramter.', error=True)
    return(out)

# Uncompress the whitelist file if compressed.
# Return the whitelist path, or False if not using a whitelist.
def prepare_whitelist(whitelist, tmpdir):
    if whitelist and testGz(whitelist):
        wlout = tmpdir + '/whitelist.tsv'
        whitelist = unGzip(whitelist, wlout)
    return whitelist

# Get list of reference names from BAM file, skipping those without reads.
def getRefs(bamFile, bedFile):
    chrNames = list()
    for line in idxstats(bamFile).strip().split('\n'):
        l = line.strip().split('\t')
        if int(l[2])>0:
            chrNames.append(l[0])
    bedChrNames = set()
    if testGz(bedFile):
        with gzopen(bedFile, 'rb') as f:
            for line in f:
                bedChrNames.add(line.decode().split('\t')[0])
    else:
        with open(bedFile, 'r') as f:
            for line in f:
                bedChrNames.add(line.split('\t')[0])
    skipChr = [x for x in chrNames if x not in bedChrNames]
    if skipChr:
        writerr('WARNING: The following references contain read alignments but are not found in the TE annotation and will be skipped: ' + ', '.join(skipChr))
        chrNames = [x for x in chrNames if x in bedChrNames]
    if chrNames:
        return chrNames
    else:
        writerr(
            """
            ERROR: Reference names not matching between BAM and TE annotation.
            If your BAM follows the ENSEMBL nomenclature (i.e. 1, 2, etc...),
            you can either change it to UCSC (chr1, chr2, etc...), or use a
            custom TE annotation with ENSEMBL chromosome names.
            """,
            error=True
        )

# Intersect reads with repeatmasker regions. Return the intersection file path.
def isec(bamFile, bedFile, whitelist, CBtag, UMItag, minOverlap, tmpdir,
         samtools, bedtools, verbose, chrom):
    refdir = tmpdir + '/refs/'
    isecdir = tmpdir + '/isec/'
    makedirs(refdir, exist_ok=True)
    makedirs(isecdir, exist_ok=True)

    refFile = refdir + chrom + '.bed.gz'
    isecFile = isecdir + chrom + '.isec.bed.gz'

    # split bed file by chromosome
    sort = 'LC_ALL=C sort -k1,1 -k2,2n --buffer-size=1G'
    if bedFile[-3:] == '.gz':
        cmd0 = f'zcat {bedFile} | gawk \'$1=="{chrom}"\' | {sort} | gzip > {refFile}'
    else:
        cmd0 = f'gawk \'$1=="{chrom}"\' {bedFile} | {sort} | gzip > {refFile}'

    # command streaming alignments for intersection
    if whitelist:
        stream = f' <({samtools} view -h {bamFile} -D {CBtag}:{whitelist} {chrom} | '
    else:
        stream = f' <({samtools} view -h {bamFile} {chrom} | '
    stream += ' gawk \'!($1~/^@/) { split("", tags); '
    stream += ' for (i=12;i<=NF;i++) {split($i,tag,":"); tags[tag[1]]=tag[3]}; '
    # Discard records without CB tag, unvalid STARSolo CBs, missing UMI tag and homopolymer UMIs
    stream += f' if(tags["{CBtag}"]~/^(|-)$/ || tags["{UMItag}"]~/^$|^(A+|G+|T+|C+|N+)$/) {{next}}; '
    # Append CB and UMI to read name
    stream += f' $1=$1"/"tags["{CBtag}"]"/"tags["{UMItag}"]; '
    stream += ' } '
    stream += ' { OFS="\\t"; print }\' | '
    stream += f' {samtools} view -u - | '
    stream += f' {bedtools} bamtobed -i stdin -bed12 -split -splitD) '

    # filter by minimum overlap between read and feature, if set
    ovfloat = ''
    ovint = ''
    if minOverlap:
        if isinstance(minOverlap, float) and 0 < minOverlap <= 1:
            ovfloat = f' -f {minOverlap} '
        elif isinstance(minOverlap, int):
            ovint = f' $NF>={minOverlap} '

    # intersection command
    cmd = f'{bedtools} intersect -a {stream} -b {refFile} '
    cmd += f' -split -bed -wo -sorted {ovfloat} | gawk -vOFS="\\t" \'{ovint} '
    # remove mate information from read name
    cmd += ' { sub(/\/[12]$/,"",$4); '
    # concatenate CB and UMI with feature name
    cmd += ' n=split($4,qname,/\//); $4=qname[n-1]"\\t"qname[n]"\\t"$16; '
    cmd += ' print $4 }\' '
    cmd += f' | gzip > {isecFile}'

    writerr(f'Extracting {chrom} reference', send=verbose)
    run_shell_cmd(cmd0)
    writerr(f'Intersecting alignments with {chrom} reference', send=verbose)
    run_shell_cmd(cmd)
    writerr(f'Finished mapping {chrom}', send=verbose)

    return isecFile

# Concatenate and sort data obtained from isec()
def chrcat(filesList, threads, outdir, tmpdir, verbose):
    makedirs(outdir, exist_ok=True)
    mappings_file = tmpdir + '/cb_umi_te.bed.gz'
    barcodes_file = outdir + '/barcodes.tsv.gz'
    features_file = outdir + '/features.tsv.gz'
    bedFiles = ' '.join(filesList)
    cmd0 = f'zcat {bedFiles} '
    cmd0 += f' | LC_ALL=C sort --parallel {threads} --buffer-size 2G '
    cmd0 += f' | gzip > {mappings_file} '
    cmd1 = f'zcat {mappings_file} | cut -f1 | uniq | gzip > {barcodes_file} '
    cmd2 = f'zcat {mappings_file} '
    cmd2 += ' | gawk \'!x[$3]++ { '
    cmd2 += ' split($3,a,"~"); '
    # avoid subfamilies with the same name
    cmd2 += ' if(a[1] in sf) { sf[a[1]]+=1 } else { sf[a[1]] }; '
    cmd2 += ' if(length(a)<2) { a[2]=a[1] }; '
    cmd2 += ' print a[1] sf[a[1]] "\\t" a[2] "\\tGene Expression" '
    cmd2 += ' }\' '
    cmd2 += f' | LC_ALL=C sort -u | gzip > {features_file} '

    writerr('Concatenating mappings', send=verbose)
    run_shell_cmd(cmd0)
    if getlen(mappings_file) == 0:
        writerr(
            f'No read-TE mappings found in {mappings_file}.'
            ' Check annotation and temporary files to troubleshoot.',
            error=True
        )
    writerr(f'Writing mapped barcodes to {barcodes_file}')
    run_shell_cmd(cmd1)
    if getlen(barcodes_file) == 0:
        writerr(
            f'No features written in {features_file}.'
            ' Check BAM format and reference annotation (e.g. chr names)'
            ' to troubleshoot.',
            error=True
        )
    writerr(f'Writing mapped features to {features_file}')
    run_shell_cmd(cmd2)
    if getlen(features_file) == 0:
        writerr(
            f'No features written in {features_file}.'
            ' Check annotation and temporary files to troubleshoot.',
            error=True
        )

    return mappings_file, barcodes_file, features_file
