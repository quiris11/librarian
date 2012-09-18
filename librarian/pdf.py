# -*- coding: utf-8 -*-
#
# This file is part of Librarian, licensed under GNU Affero GPLv3 or later.
# Copyright © Fundacja Nowoczesna Polska. See NOTICE for more information.
#
from __future__ import with_statement
import os
import os.path
import shutil
from StringIO import StringIO
from tempfile import mkdtemp, NamedTemporaryFile
import re
from copy import deepcopy
from subprocess import call, PIPE

from Texml.processor import process
from lxml import etree
from lxml.etree import XMLSyntaxError, XSLTApplyError

from librarian.dcparser import Person
from librarian.parser import WLDocument
from librarian import ParseError, DCNS, get_resource, OutputFile
from librarian import functions
from librarian.cover import ImageCover as WLCover


functions.reg_substitute_entities()
functions.reg_strip()
functions.reg_starts_white()
functions.reg_ends_white()
functions.reg_texcommand()

STYLESHEETS = {
    'wl2tex': 'pdf/wl2tex.xslt',
}

#CUSTOMIZATIONS = [
#    'nofootnotes',
#    'nothemes',
#    'defaultleading',
#    'onehalfleading',
#    'doubleleading',
#    'nowlfont',
#    ]

def insert_tags(doc, split_re, tagname, exclude=None):
    """ inserts <tagname> for every occurence of `split_re' in text nodes in the `doc' tree

    >>> t = etree.fromstring('<a><b>A-B-C</b>X-Y-Z</a>');
    >>> insert_tags(t, re.compile('-'), 'd');
    >>> print etree.tostring(t)
    <a><b>A<d/>B<d/>C</b>X<d/>Y<d/>Z</a>
    """

    for elem in doc.iter(tag=etree.Element):
        if exclude and elem.tag in exclude:
            continue
        if elem.text:
            chunks = split_re.split(elem.text)
            while len(chunks) > 1:
                ins = etree.Element(tagname)
                ins.tail = chunks.pop()
                elem.insert(0, ins)
            elem.text = chunks.pop(0)
        if elem.tail:
            chunks = split_re.split(elem.tail)
            parent = elem.getparent()
            ins_index = parent.index(elem) + 1
            while len(chunks) > 1:
                ins = etree.Element(tagname)
                ins.tail = chunks.pop()
                parent.insert(ins_index, ins)
            elem.tail = chunks.pop(0)


def substitute_hyphens(doc):
    insert_tags(doc,
                re.compile("(?<=[^-\s])-(?=[^-\s])"),
                "dywiz",
                exclude=[DCNS("identifier.url"), DCNS("rights.license")]
                )


def fix_hanging(doc):
    insert_tags(doc,
                re.compile("(?<=\s\w)\s+"),
                "nbsp",
                exclude=[DCNS("identifier.url"), DCNS("rights.license")]
                )


def move_motifs_inside(doc):
    """ moves motifs to be into block elements """
    for master in doc.xpath('//powiesc|//opowiadanie|//liryka_l|//liryka_lp|//dramat_wierszowany_l|//dramat_wierszowany_lp|//dramat_wspolczesny'):
        for motif in master.xpath('motyw'):
            for sib in motif.itersiblings():
                if sib.tag not in ('sekcja_swiatlo', 'sekcja_asterysk', 'separator_linia', 'begin', 'end', 'motyw', 'extra', 'uwaga'):
                    # motif shouldn't have a tail - it would be untagged text
                    motif.tail = None
                    motif.getparent().remove(motif)
                    sib.insert(0, motif)
                    break


def hack_motifs(doc):
    """ dirty hack for the marginpar-creates-orphans LaTeX problem
    see http://www.latex-project.org/cgi-bin/ltxbugs2html?pr=latex/2304

    moves motifs in stanzas from first verse to second
    and from next to last to last, then inserts negative vspace before them
    """
    for motif in doc.findall('//strofa//motyw'):
        # find relevant verse-level tag
        verse, stanza = motif, motif.getparent()
        while stanza is not None and stanza.tag != 'strofa':
            verse, stanza = stanza, stanza.getparent()
        breaks_before = sum(1 for i in verse.itersiblings('br', preceding=True))
        breaks_after = sum(1 for i in verse.itersiblings('br'))
        if (breaks_before == 0 and breaks_after > 0) or breaks_after == 1:
            move_by = 1
            if breaks_after == 2:
                move_by += 1
            moved_motif = deepcopy(motif)
            motif.tag = 'span'
            motif.text = None
            moved_motif.tail = None
            moved_motif.set('moved', str(move_by))

            for br in verse.itersiblings('br'):
                if move_by > 1:
                    move_by -= 1
                    continue
                br.addnext(moved_motif)
                break


def parse_creator(doc):
    """ find all dc:creator and dc.contributor tags and add *_parsed versions with forenames first """
    for person in doc.xpath("|".join('//dc:'+(tag) for tag in (
                    'creator', 'contributor.translator', 'contributor.editor', 'contributor.technical_editor')),
                    namespaces = {'dc': str(DCNS)})[::-1]:
        if not person.text:
            continue
        p = Person.from_text(person.text)
        person_parsed = deepcopy(person)
        person_parsed.tag = person.tag + '_parsed'
        person_parsed.set('sortkey', person.text)
        person_parsed.text = p.readable()
        person.getparent().insert(0, person_parsed)


def get_stylesheet(name):
    return get_resource(STYLESHEETS[name])


def package_available(package, args='', verbose=False):
    """ check if a verion of a latex package accepting given args is available """
    tempdir = mkdtemp('-wl2pdf-test')
    fpath = os.path.join(tempdir, 'test.tex')
    f = open(fpath, 'w')
    f.write(r"""
        \documentclass{wl}
        \usepackage[%s]{%s}
        \begin{document}
        \end{document}
        """ % (args, package))
    f.close()
    if verbose:
        p = call(['xelatex', '-output-directory', tempdir, fpath])
    else:
        p = call(['xelatex', '-interaction=batchmode', '-output-directory', tempdir, fpath], stdout=PIPE, stderr=PIPE)
    shutil.rmtree(tempdir)
    return p == 0


def transform(wldoc, verbose=False, save_tex=None, morefloats=None,
              cover=None, flags=None, customizations=None,
              imgdir=""):
    """ produces a PDF file with XeLaTeX

    wldoc: a WLDocument
    verbose: prints all output from LaTeX
    save_tex: path to save the intermediary LaTeX file to
    morefloats (old/new/none): force specific morefloats
    cover: a cover.Cover object
    flags: less-advertising,
    customizations: user requested customizations regarding various formatting parameters (passed to wl LaTeX class)
    """

    # Parse XSLT
    try:
        document = load_including_children(wldoc)

        if cover:
            if cover is True:
                cover = WLCover
            the_cover = cover(document.book_info)
            document.edoc.getroot().set('data-cover-width', str(the_cover.width))
            document.edoc.getroot().set('data-cover-height', str(the_cover.height))
            if the_cover.uses_dc_cover:
                if document.book_info.cover_by:
                    document.edoc.getroot().set('data-cover-by', document.book_info.cover_by)
                if document.book_info.cover_source:
                    document.edoc.getroot().set('data-cover-source', document.book_info.cover_source)
        if flags:
            for flag in flags:
                document.edoc.getroot().set('flag-' + flag, 'yes')

        # check for LaTeX packages
        if morefloats:
            document.edoc.getroot().set('morefloats', morefloats.lower())
        elif package_available('morefloats', 'maxfloats=19'):
            document.edoc.getroot().set('morefloats', 'new')

        # add customizations
        if customizations is not None:
            document.edoc.getroot().set('customizations', u','.join(customizations))

        # hack the tree
        #move_motifs_inside(document.edoc)
        #hack_motifs(document.edoc)
        parse_creator(document.edoc)
        if document.book_info.language == 'pol':
            substitute_hyphens(document.edoc)
        fix_hanging(document.edoc)

        # wl -> TeXML
        style_filename = get_stylesheet("wl2tex")
        style = etree.parse(style_filename)

        texml = document.transform(style)
        etree.dump(texml.getroot())
        # TeXML -> LaTeX
        temp = mkdtemp('-wl2pdf')

        if cover:
            with open(os.path.join(temp, 'cover.jpg'), 'w') as f:
                the_cover.save(f)

        for img in document.edoc.findall('//ilustr'):
            shutil.copy(os.path.join(imgdir, img.get('src')), temp)


        del document # no longer needed large object :)

        tex_path = os.path.join(temp, 'doc.tex')
        fout = open(tex_path, 'w')
        process(StringIO(texml), fout, 'utf-8')
        fout.close()
        del texml

        if save_tex:
            shutil.copy(tex_path, save_tex)

        # LaTeX -> PDF
        shutil.copy(get_resource('pdf/wl.cls'), temp)
        shutil.copy(get_resource('res/wl-logo.png'), temp)
        shutil.copy('logo.eps', temp)

        cwd = os.getcwd()
        os.chdir(temp)

        if verbose:
            p = call(['xelatex', tex_path])
        else:
            p = call(['xelatex', '-interaction=batchmode', tex_path], stdout=PIPE, stderr=PIPE)
        if p:
            raise ParseError("Error parsing .tex file")

        os.chdir(cwd)

        output_file = NamedTemporaryFile(prefix='librarian', suffix='.pdf', delete=False)
        pdf_path = os.path.join(temp, 'doc.pdf')
        shutil.move(pdf_path, output_file.name)
        shutil.rmtree(temp)
        return OutputFile.from_filename(output_file.name)

    except (XMLSyntaxError, XSLTApplyError), e:
        print e
        raise ParseError(e)


def load_including_children(wldoc=None, provider=None, uri=None):
    """ Makes one big xml file with children inserted at end.
    
    Either wldoc or provider and URI must be provided.
    """

    if uri and provider:
        f = provider.by_uri(uri)
        text = f.read().decode('utf-8')
        f.close()
    elif wldoc is not None:
        text = etree.tostring(wldoc.edoc, encoding=unicode)
        provider = wldoc.provider
    else:
        raise ValueError('Neither a WLDocument, nor provider and URI were provided.')

    text = re.sub(ur"([\u0400-\u04ff]+)", ur"<alien>\1</alien>", text)

    document = WLDocument.from_string(text, parse_dublincore=True)
    document.swap_endlines()

    for child_uri in document.book_info.parts:
        child = load_including_children(provider=provider, uri=child_uri)
        document.edoc.getroot().append(child.edoc.getroot())
    return document
