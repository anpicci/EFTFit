try:
    import ROOT
except ImportError:
    ROOT = None  # The maintained 1D consumer uses uproot and Matplotlib.
import logging
import os
import sys
import numpy
import itertools
import subprocess as sp
import os
from EFTFit.Fitter.execution_provenance import resolve_canonical_aggregate
import numpy as np

from EFTFit.Fitter.scan_1d import (
    read_scan, combine_scans, interval_width_ratio,
    canonicalize_entries, ScanEntry,
)
from EFTFit.Fitter.scan_1d_plot import DisplayConfig, render_curves, render_summary
from EFTFit.Fitter.wc_fit_config import load_fit_config, load_fit_input_package
import warnings
from dataclasses import replace

class EFTPlot(object):
    def __init__(self, wc_ranges=None, *, fit_config=None, fit_package_root=None,
                 display_config=None, result_options=None):
        self.logger = logging.getLogger(__name__)
        self.ContourHelper = None
        if ROOT is not None:
            from EFTFit.Fitter.ContourHelper import ContourHelper
            self.ContourHelper = ContourHelper()
        if fit_config is not None and fit_package_root is not None:
            raise ValueError("supply fit_config or fit_package_root, not both")
        self.fit_config = (load_fit_input_package(fit_package_root).fit_config
                           if fit_package_root is not None else
                           load_fit_config(fit_config) if fit_config is not None else None)
        self.display = DisplayConfig.from_mapping(display_config)
        self.result_options = dict(result_options or {})
        if self.fit_config is not None:
            self.wcs = list(self.fit_config.scientific_wcs)
            self.wc_ranges = dict(self.fit_config.wc_ranges)
            if wc_ranges is not None and {key: tuple(value) for key, value in wc_ranges.items()} != self.wc_ranges:
                raise ValueError("wc_ranges conflicts with package/config scientific authority")
            self.metadata_authority = "fit_config"
        else:
            self.wc_ranges = dict(wc_ranges or {})
            self.wcs = list(self.wc_ranges)
            self.metadata_authority = "legacy_non_authoritative"
            self.display = replace(self.display, title="Legacy/non-authoritative " + self.display.title)
            warnings.warn("EFTPlot without fit_config/fit_package_root is legacy/non-authoritative; supply explicit WCs or package metadata", RuntimeWarning)
        self.wcs_pairs = list(itertools.combinations(self.wcs, 2))
        self.SMMus = ['mu_ttll','mu_ttlnu','mu_ttH','mu_tllq']
        self.sm_ranges = {  'mu_ttH':(0,7),   'mu_ttlnu':(0,3)
                         }
        self.histosFileName = 'Histos.root'
        self.texdic = {
            #'ctW': '\it{c}_{\mathrm{tW}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'ctZ': '\it{c}_{\mathrm{tZ}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'ctp': '\it{c}_{\mathrm{t} \\varphi}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'cpQM': '\it{c}^{-}_{\\varphi \mathrm{Q}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'ctG': '\it{c}_{\mathrm{tG}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'cbW': '\it{c}_{\mathrm{bW}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'cpQ3': '\it{c}^{3}_{\\varphi \mathrm{Q}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'cptb': '\it{c}_{\\varphi \mathrm{tb}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'cpt': '\it{c}_{\\varphi \mathrm{t}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'cQl3': '\it{c}^{3(\\ell)}_{\mathrm{Q}\\ell}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'cQlM': '\it{c}^{-(\\ell)}_{\mathrm{Q}\\ell}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'cQe': '\it{c}^{(\\ell)}_{\mathrm{Qe}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'ctl': '\it{c}^{(\\ell)}_{\mathrm{t}\\ell}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'cte': '\it{c}^{(\\ell)}_{\mathrm{te}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'ctlS': '\it{c}^{S(\\ell)}_{\mathrm{t}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'ctlT': '\it{c}^{T(\\ell)}_{\mathrm{t}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'cQq81': '\it{c}^{18}_{\mathrm{Qq}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'cQq11': '\it{c}^{11}_{\mathrm{Qq}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'ctq8': '\it{c}^{8}_{\mathrm{tq}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'ctq1': '\it{c}^{1}_{\mathrm{tq}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'cQq13': '\it{c}^{31}_{\mathrm{Qq}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'cQq83': '\it{c}^{38}_{\mathrm{Qq}}/\mathrm{\Lambda^{2} [TeV^{-2}]}', 
            #'ctt1': '\it{c}^{1}_{\mathrm{tt}}/\mathrm{\Lambda^{2} [TeV^{-2}]}',
            #'cQQ1': '\it{c}^{1}_{\mathrm{QQ}}/\mathrm{\Lambda^{2} [TeV^{-2}]}',
            #'cQt8': '\it{c}^{8}_{\mathrm{Qt}}/\mathrm{\Lambda^{2} [TeV^{-2}]}',
            #'cQt1': '\it{c}^{1}_{\mathrm{Qt}}/\mathrm{\Lambda^{2} [TeV^{-2}]}'

            'ctW' : r'#it{c}_{tW}/#Lambda^{2} [TeV^{-2}]',
            'ctZ' : r'#it{c}_{tZ}/#Lambda^{2} [TeV^{-2}]',
            'ctp' : r'#it{c}_{t#varphi}/#Lambda^{2} [TeV^{-2}]',
            'cpQM': r'#it{c}^{(-)}_{#varphi Q}/#Lambda^{2} [TeV^{-2}]',
            'ctG' : r'#it{c}_{tG}/#Lambda^{2} [TeV^{-2}]',
            'cbW' : r'#it{c}_{bW}/#Lambda^{2} [TeV^{-2}]',
            'cpQ3': r'#it{c}^{(3)}_{#varphi Q}/#Lambda^{2} [TeV^{-2}]',
            'cptb': r'#it{c}_{#varphi tb}/#Lambda^{2} [TeV^{-2}]',
            'cpt' : r'#it{c}_{#varphi t}/#Lambda^{2} [TeV^{-2}]',

            'cQl3': r'#it{c}^{(3,l)}_{Ql}/#Lambda^{2} [TeV^{-2}]',
            'cQlM': r'#it{c}^{(-,l)}_{Ql}/#Lambda^{2} [TeV^{-2}]',
            'cQe' : r'#it{c}^{(l)}_{Qe}/#Lambda^{2} [TeV^{-2}]',
            'ctl' : r'#it{c}^{(l)}_{tl}/#Lambda^{2} [TeV^{-2}]',
            'cte' : r'#it{c}^{(l)}_{te}/#Lambda^{2} [TeV^{-2}]',
            'ctlS': r'#it{c}^{(S,l)}_{t}/#Lambda^{2} [TeV^{-2}]',
            'ctlT': r'#it{c}^{(T,l)}_{t}/#Lambda^{2} [TeV^{-2}]',

            'cQq81': r'#it{c}^{(1,8)}_{Qq}/#Lambda^{2} [TeV^{-2}]',
            'cQq11': r'#it{c}^{(1,1)}_{Qq}/#Lambda^{2} [TeV^{-2}]',
            'ctq8' : r'#it{c}^{(8)}_{tq}/#Lambda^{2} [TeV^{-2}]',
            'ctq1' : r'#it{c}^{(1)}_{tq}/#Lambda^{2} [TeV^{-2}]',
            'cQq13': r'#it{c}^{(3,1)}_{Qq}/#Lambda^{2} [TeV^{-2}]',
            'cQq83': r'#it{c}^{(3,8)}_{Qq}/#Lambda^{2} [TeV^{-2}]',

            'ctt1': r'#it{c}^{(1)}_{tt}/#Lambda^{2} [TeV^{-2}]',
            'cQQ1': r'#it{c}^{(1)}_{QQ}/#Lambda^{2} [TeV^{-2}]',
            'cQt8': r'#it{c}^{(8)}_{Qt}/#Lambda^{2} [TeV^{-2}]',
            'cQt1': r'#it{c}^{(1)}_{Qt}/#Lambda^{2} [TeV^{-2}]',

            #'cbW' : r'#it{c}_{bW}/#Lambda^{2} [TeV^{-2}]',
            #'cpQ3': r'#it{c}^{(3)}_{#varphi Q}/#Lambda^{2} [TeV^{-2}]',

        }
        self.texdicfrac = {
            'ctW': '\it{c}_{\mathrm{tW}}',
            'ctZ': '\it{c}_{\mathrm{tZ}}',
            'ctp': '\it{c}_{\mathrm{t} \\varphi}',
            'cpQM': '\it{c}^{-}_{\\varphi \mathrm{Q}}',
            'ctG': '\it{c}_{\mathrm{tG}}',
            'cbW': '\it{c}_{\mathrm{bW}}',
            'cpQ3': '\it{c}^{3}_{\\varphi \mathrm{Q}}',
            'cptb': '\it{c}_{\\varphi \mathrm{tb}}',
            'cpt': '\it{c}_{\\varphi \mathrm{t}}',
            'cQl3': '\it{c}^{3(\\ell)}_{\mathrm{Q}\\ell}',
            'cQlM': '\it{c}^{-(\\ell)}_{\mathrm{Q}\\ell}',
            'cQe': '\it{c}^{(\\ell)}_{\mathrm{Qe}}',
            'ctl': '\it{c}^{(\\ell)}_{\mathrm{t}\\ell}',
            'cte': '\it{c}^{(\\ell)}_{\mathrm{te}}',
            'ctlS': '\it{c}^{S(\\ell)}_{\mathrm{t}}',
            'ctlT': '\it{c}^{T(\\ell)}_{\mathrm{t}}',
            'cQq81': '\it{c}^{18}_{\mathrm{Qq}}',
            'cQq11': '\it{c}^{11}_{\mathrm{Qq}}',
            'ctq8': '\it{c}^{8}_{\mathrm{tq}}',
            'ctq1': '\it{c}^{1}_{\mathrm{tq}}',
            'cQq13': '\it{c}^{31}_{\mathrm{Qq}}',
            'cQq83': '\it{c}^{38}_{\mathrm{Qq}}',
            'ctt1': '\it{c}^{1}_{\mathrm{tt}}',
            'cQQ1': '\it{c}^{1}_{\mathrm{QQ}}',
            'cQt8': '\it{c}^{8}_{\mathrm{Qt}}',
            'cQt1': '\it{c}^{1}_{\mathrm{Qt}}'
        }
        self.texdicmacro = {
            'ctW': '\ctW',
            'ctZ': '\ctZ',
            'ctp': '\ctp',
            'cpQM': '\cpQM',
            'ctG': '\ctG',
            'cbW': '\cbW',
            'cpQ3': '\cpQa',
            'cptb': '\cptb',
            'cpt': '\cpt',
            'cQl3i': '\cQla',
            'cQlMi': '\cQlM',
            'cQei': '\cQe',
            'ctli': '\ctl',
            'ctei': '\cte',
            'ctlSi': '\ctlS',
            'ctlTi': '\ctlT',
            'cQq81': '\cQqEightOne',
            'cQq11': '\cQqOneOne',
            'ctq8': '\ctqEight',
            'ctq1': '\ctqOne',
            'cQq13': '\cQqOneThree',
            'cQq83': '\cQqEightThree',
            'ctt1': '\cttOne',
            'cQQ1': '\cQQOne',
            'cQt8': '\cQtEight',
            'cQt1': '\cQtOne',
        }
        self.texdicrev = {v: k for k,v in self.texdic.items()}

        # Legacy ROOT surfaces share caller-owned display text; no analysis defaults.
        self.lumi = self.display.luminosity
        self.arXiv = self.display.reference
        self.luminosity_text = " ".join(value for value in (self.display.luminosity, self.display.collision_energy) if value)
        if ROOT is not None:
            self.CMS_text = ROOT.TLatex(0.88, 0.895, self.display.title)
            self.CMS_extra = ROOT.TLatex(0.88, 0.865, "")
            self.Lumi_text = ROOT.TLatex(0.9, 0.91, self.luminosity_text)
            for text in (self.CMS_text, self.CMS_extra, self.Lumi_text):
                text.SetNDC(1)
                text.SetTextSize(0.04)

    def read_scan_result(self, base_name_lst, wc, **kwargs):
        """Structured consumer; canonical aggregate resolution remains authoritative."""
        if not wc or (self.fit_config is not None and wc not in self.fit_config.scientific_wcs):
            raise ValueError("scan WC must be supplied and belong to the configured scientific population")
        options = dict(self.result_options)
        options.update(kwargs)
        directory = options.pop('dir_path', '../fit_files')
        postfix = options.pop('postfix', '')
        job_set_ids = options.pop('job_set_ids', {})
        common_reference = options.pop('common_reference', False)
        failed_point_policy = options.pop('failed_point_policy', 'mask')
        family = options.pop('family', 'unspecified')
        if options:
            raise TypeError("unknown result options: {}".format(sorted(options)))
        names = [base_name_lst] if isinstance(base_name_lst, str) else list(base_name_lst)
        if not names or any(not isinstance(name, str) or not name for name in names):
            raise ValueError("nonempty result basename list required")
        paths = []
        for name in names:
            if postfix:
                if job_set_ids:
                    raise ValueError("postfix paths cannot verify canonical job_set_ids")
                result_path = os.path.join(directory, 'higgsCombine{}.MultiDimFit{}.root'.format(name, postfix))
            else:
                result_path = resolve_canonical_aggregate(name, job_set_ids.get(name), directory)
            paths.append(result_path)
        return combine_scans([read_scan(path, wc, failed_point_policy=failed_point_policy, family=family) for path in paths], common_reference=common_reference)

    def _selected_wcs(self, values):
        selected = list(values or self.wcs)
        if not selected:
            raise ValueError("supply explicit WCs or package fit configuration")
        if len(selected) != len(set(selected)):
            raise ValueError("duplicate requested WCs")
        if self.fit_config is not None and set(selected) - set(self.fit_config.scientific_wcs):
            raise ValueError("requested WC is outside configured scientific population")
        return selected

    # Appends a given string to each item in a list of strings
    def AppendStrToItemsInLst(self,in_lst,in_str):
        out_lst = []
        for item in in_lst:
            if type(item) is not str:
                raise Exception("Error: Cannot append the str to items in this list, not all items are str")
            out_lst.append(item+in_str)
        return out_lst



    # Takes as input the name of a root file (assumed to be in ../fit_files)
    # Retruns [wc vals in the scan, delta nll vals at each point]
    # Optionally removes duplicate wc points (choosing min nll)
    def GetWCsNLLFromRoot(self,base_name_lst,wc,unique=False,**kwargs):
        """Compatibility arrays: raw 2*deltaNLL, including special fit entries.

        unique=True returns sorted coordinates with minimum raw deltaNLL.
        Structured metadata and normalized grid curves: read_scan_result().
        """
        scan = self.read_scan_result(base_name_lst, wc, **kwargs)
        if scan.invalid_entries:
            raise ValueError("plain arrays cannot preserve invalid-point topology/provenance; use read_scan_result")
        entries = scan.valid_points if unique else scan.raw_entries
        return [[entry.coordinate for entry in entries], [2 * entry.delta_nll for entry in entries]]



    def ResetHistoFile(self, name=''):
        ROOT.TFile('Histos{}.root'.format(name),'RECREATE')
        self.histosFileName = 'Histos{}.root'.format(name)



    def LLPlot1DEFT(self, name_lst=['.test'], frozen=False, wc='', log=False, final=False, **kwargs):
        kwargs.setdefault('family', 'frozen' if frozen else 'profiled')
        scan = self.read_scan_result(name_lst, wc, **kwargs)
        name = '{}1DNLL{}'.format(wc, '_log' if log else '')
        rendered = render_curves((scan,), ('Others fixed' if frozen else 'Others profiled',),
                                 self.display, name, thresholds=(1, 4), ceiling=10, log=log)
        return dict(rendered, scans=(scan,))


    def duplicates(self, seq, item):
        start_at = -1
        locs = []
        while True:
            try:
                loc = seq.index(item,start_at+1)
            except ValueError:
                break
            else:
                locs.append(loc)
                start_at = loc
        return locs

    def clean_duplicates(self, graphwcs, graphnlls):
        if len(graphwcs) != len(graphnlls):
            raise ValueError("coordinate and likelihood lengths differ")
        scan = canonicalize_entries('coordinate', [ScanEntry(x, y / 2, 'grid') for x, y in zip(graphwcs, graphnlls)], failed_point_policy='error')
        return [[point.coordinate for point in scan.points], [2 * point.delta_nll for point in scan.points]]

    def OverlayLLPlot1DEFT(self,**kwargs):
        names = (kwargs.pop('name1_lst', ['.test']), kwargs.pop('name2_lst', ['.test']))
        wc = kwargs.pop('wc', '')
        log = kwargs.pop('log', False)
        final = kwargs.pop('final', False)
        filename = kwargs.pop('filename', '')
        titles = kwargs.pop('titles', ['Other WCs profiled', 'Other WCs fixed to SM'])
        thresholds = tuple(kwargs.pop('nll_{}sigma'.format(index), index ** 2) for index in (1, 2, 3))
        ceiling = kwargs.pop('ceiling', 12)
        common_reference = kwargs.pop('common_reference', self.result_options.get('common_reference', False))
        failed_point_policy = kwargs.pop('failed_point_policy', self.result_options.get('failed_point_policy', 'mask'))
        options = []
        for family in (1, 2):
            current = {'common_reference': common_reference, 'failed_point_policy': failed_point_policy,
                       'family': 'profiled' if family == 1 else 'frozen'}
            for public, internal in (('dir', 'dir_path'), ('pf', 'postfix'), ('job_set_ids', 'job_set_ids')):
                key = public + str(family)
                if key in kwargs:
                    current[internal] = kwargs.pop(key)
            options.append(current)
        if kwargs:
            raise TypeError("unknown overlay options: {}".format(sorted(kwargs)))
        scans = tuple(self.read_scan_result(family_names, wc, **family_options)
                      for family_names, family_options in zip(names, options))
        suffix = '_log' if log else '_final' if final else '_prelim'
        rendered = render_curves(scans, titles, self.display, 'Overlay{}1DNLL{}{}'.format(wc, filename, suffix),
                                 thresholds=thresholds, ceiling=ceiling, log=log)
        return dict(rendered, scans=scans)

    def OverlayZoomLLPlot1DEFT(self, name1='.test', name2='.test', wc='', log=False):
        return self.OverlayLLPlot1DEFT(name1_lst=[name1], name2_lst=[name2], wc=wc, log=log, ceiling=4)

    def BatchLLPlot1DEFT(self, basename_lst=['.test'], frozen=False, wcs=[], log=False, **kwargs):
        names = [basename_lst] if isinstance(basename_lst, str) else basename_lst
        return [self.LLPlot1DEFT(self.AppendStrToItemsInLst(names, '.' + wc), frozen, wc, log, **kwargs)
                for wc in self._selected_wcs(wcs)]

    def BatchLLPlotNDEFT(self, basename='.test', frozen=False, wcs=[], log=False):
        if not wcs:
            wcs = self.wcs

        ROOT.gROOT.SetBatch(True)

        #wcs.remove('ctG')
        for pair in zip(wcs[::2], wcs[1::2]):
            self.LLPlot2DEFT(basename, wcs=pair, log=log, ceiling=300)

    def BatchOverlayLLPlot1DDNN(self, basename1_lst=['.EFT.SM.Float'], basename2_lst=['.EFT.SM.Freeze'], wcs=[], log=False, final=False, titles=['Others profiled', 'Others fixed to SM']):
        if (type(basename1_lst) is not list) or (type(basename2_lst) is not list): raise Exception("Error: Pass the name of the file as a list (even if it's just of length 1)")
        if not wcs:
            wcs = self.wcs

        ROOT.gROOT.SetBatch(True)

        for wc in wcs:
            print(wc)
            self.OverlayLLPlot1DEFT(name1_lst=basename1_lst, name2_lst=basename2_lst, wc=wc, log=log, final=final, titles=titles)

    def BatchOverlayLLPlot1DEFT(self, basename1_lst=['.EFT.SM.Float'], basename2_lst=['.EFT.SM.Freeze'], wcs=[], log=False, final=False, titles=['Others profiled', 'Others fixed to SM'], **kwargs):
        names1 = [basename1_lst] if isinstance(basename1_lst, str) else basename1_lst
        names2 = [basename2_lst] if isinstance(basename2_lst, str) else basename2_lst
        return [self.OverlayLLPlot1DEFT(
            name1_lst=self.AppendStrToItemsInLst(names1, '.' + wc),
            name2_lst=self.AppendStrToItemsInLst(names2, '.' + wc),
            wc=wc, log=log, final=final, titles=titles, **kwargs)
            for wc in self._selected_wcs(wcs)]

    def BatchOverlayZoomLLPlot1DEFT(self, basename1='.EFT.SM.Float', basename2='.EFT.SM.Freeze', wcs=[], log=False):
        if not wcs:
            wcs = self.wcs

        ROOT.gROOT.SetBatch(True)

        for wc in wcs:
            self.OverlayZoomLLPlot1DEFT(basename1+'.'+wc, basename2+'.'+wc, wc, log)

    def LLPlot2DEFT(self, name='.test', wcs=[], ceiling=1, log=False, final=False):
        if len(wcs)!=2:
            logging.error("Function 'LLPlot2D' requires exactly two wcs!")
            return
        if not os.path.exists('../fit_files/higgsCombine{}.MultiDimFit.root'.format(name)):
            logging.error("File higgsCombine{}.MultiDimFit.root does not exist!".format(name))
            return

        ROOT.gROOT.SetBatch(True)

        canvas = ROOT.TCanvas('c','c',800,800)

        # Open file and draw 2D histogram
        # wcs[0] is y-axis variable, wcs[1] is x-axis variable
        rootFile = ROOT.TFile.Open('../fit_files/higgsCombine{}.MultiDimFit.root'.format(name))
        limitTree = rootFile.Get('limit')
        hname = '{}{}less{}'.format(wcs[0],wcs[1],ceiling)
        if log:
            hname += "_log"
        minZ = limitTree.GetMinimum('deltaNLL')

        points = 100
        hist = ROOT.TH3F('hist', hname, points, self.wc_ranges[wcs[1]][0], self.wc_ranges[wcs[1]][1], points, self.wc_ranges[wcs[0]][0], self.wc_ranges[wcs[0]][1], 100, 0, ceiling)
        #hist = ROOT.TH2F('hist', hname, points, self.wc_ranges[wcs[1]][0], self.wc_ranges[wcs[1]][1], points, self.wc_ranges[wcs[0]][0], self.wc_ranges[wcs[0]][1])
        limitTree.Draw('2*(deltaNLL-{}):{}:{}>>+hist'.format(minZ,wcs[0],wcs[1]), '2*(deltaNLL-{})<{}'.format(minZ,ceiling))
        #limitTree.Draw('2*(deltaNLL-{}):{}:{}>>hist({},{},{},{},{},{})'.format(minZ,wcs[1],wcs[0],points,self.wc_ranges[wcs[0]][0],self.wc_ranges[wcs[0]][1],points,self.wc_ranges[wcs[1]][0],self.wc_ranges[wcs[1]][1]), '2*(deltaNLL-{})<{}'.format(minZ,ceiling), 'prof colz')
        hist = canvas.GetPrimitive("hist")
        hist = hist.Project3DProfile()
        hist.SetContour(points)
        ROOT.gStyle.SetOptStat(0)
        hist.Draw('colz')
        hist.SetTitle(';{};{}'.format(wcs[0],wcs[1]))

        # Change plot formats
        if log:
            canvas.SetLogz()
        hist.GetYaxis().SetTitle(self.texdic[wcs[1].rstrip('i')])
        hist.GetXaxis().SetTitle(self.texdic[wcs[0].rstrip('i')])
        hist.SetTitle('')#"2*deltaNLL < {}".format(ceiling))
        hist.SetStats(0)

        ROOT.gStyle.SetOptStat(0)

        # CMS-required text
        self.CMS_text = ROOT.TLatex(0.1, 0.945, "CMS")# Simulation")
        self.CMS_text.SetNDC(1)
        self.CMS_text.SetTextSize(0.04)
        self.CMS_text.SetTextAlign(13)
        self.CMS_text.Draw('same')
        if not final: self.CMS_extra = ROOT.TLatex(0.2, 0.945, "Preliminary")# Simulation")
        #self.CMS_extra = ROOT.TLatex(0.2, 0.945, "Supplementary")# Simulation")
        self.CMS_extra.SetNDC(1)
        self.CMS_extra.SetTextSize(0.04)
        self.CMS_extra.SetTextAlign(13)
        self.CMS_extra.SetTextFont(52)
        if not final: self.CMS_extra.Draw('same')
        self.Lumi_text = ROOT.TLatex(0.9, 0.91, self.luminosity_text)
        self.Lumi_text.SetNDC(1)
        self.Lumi_text.SetTextSize(0.04)
        self.Lumi_text.SetTextAlign(30)
        self.Lumi_text.SetTextFont(42)
        self.Lumi_text.Draw('same')

        # Save plot
        canvas.Print(hname+".png",'png')
        canvas.Print(hname+".eps",'eps')
        os.system('ps2pdf -dPDFSETTINGS=/prepress -dEPSCrop {}.eps {}.pdf'.format(hname,hname))

        # Save to root file
        # Log settings don't save to the histogram, so redundant to save those
        if not log:
            outfile = ROOT.TFile(self.histosFileName,'UPDATE')
            hist.Write()
            outfile.Close()
        hist.SetDirectory(0)
        #return hist

    '''
    Example:
    plotter.LLPlot2DVarEFT(['.100322.EFT.Frozen.top19001.100.10.n10p0.ctp.cpt', '.100322.EFT.Frozen.top19001.100.n10p0.ctp.cpt', '.100322.EFT.Frozen.top19001.10.ctp.cpt', '.100322.EFT.Frozen.top19001.100.ctp.cpt'], wcs=['ctp','cpt'], edge_x=[-11, -10, 0, 35], edge_y=[-5, 5], ceiling=10)
    '''
    def LLPlot2DVarEFT(self, names=['.test'], wcs=[], ceiling=1, log=False, final=False, edge_x = [-11,10,0,35], edge_y = [-5,5], bins_x=[10, 100], bins_y=[10]):
        if type(names) == str: names = [names]
        if len(wcs)!=2:
            logging.error("Function 'LLPlot2DVar' requires exactly two wcs!")
            return
        for name in names:
            if not os.path.exists('../fit_files/higgsCombine{}.MultiDimFit.root'.format(name)):
                logging.error("File higgsCombine{}.MultiDimFit.root does not exist!".format(name))
                return

        # Setup ROOT stuff
        ROOT.gROOT.SetBatch(True)
        canvas = ROOT.TCanvas('c','c',800,800)
 
        # Error checking
        if len(edge_x) % 2 > 0: raise Exception('Please specify an even number of x bin edges!')
        if len(edge_y) % 2 > 0: raise Exception('Please specify an even number of y bin edges!')
        if len(edge_x) % len(bins_x) > 0 or (len(bins_x) == 1 and len(edge_x) != 2): raise Exception('Please specify ' + str(len(edge_x)/2) + ' bins for x (' + ','.join([str(e) for e in edge_x]) + ')!')
        if len(edge_y) % len(bins_y) > 0 or (len(bins_y) == 1 and len(edge_y) != 2): raise Exception('Please specify ' + str(len(edge_y)/2) + ' bins for y (' + ','.join([str(e) for e in edge_y]) + ')!')

        # Build arrays for bins
        nedge = int(len(edge_x)/2)
        print(nedge)
        print(type(nedge))
        full_edge_x = edge_x
        full_edge_y = edge_y
        edge_x = np.linspace(full_edge_x[0], full_edge_x[-1], bins_x[0])
        for edge in range(1, nedge):
            edge_x = np.append(edge_x, np.linspace(full_edge_x[edge], full_edge_x[-1 - edge], bins_x[edge]))
        nedge = int(len(edge_y)/2)
        edge_y = np.linspace(full_edge_y[0], full_edge_y[-1], bins_y[0])
        for edge in range(1, nedge):
            edge_y = np.append(edge_y, np.linspace(full_edge_y[edge], full_edge_y[-1 - edge], bins_y[edge]))
            
        edge_x = np.sort(edge_x)
        edge_y = np.sort(edge_y)
        #edge_x = np.unique(np.sort(edge_x))
        #edge_y = np.unique(np.sort(edge_y))
        z = np.linspace(0, ceiling, ceiling*10)

        # hadd all inputs
        snames = ' '.join(['../fit_files/higgsCombine{}.MultiDimFit.root'.format(name) for name in names])
        os.system('hadd -f ../fit_files/higgsCombine.var{}.MultiDimFit.root {}'.format(names[0], snames))
        rootFile = ROOT.TFile.Open('../fit_files/higgsCombine.var{}.MultiDimFit.root'.format(names[0]))

        # Get limits
        limitTree = rootFile.Get('limit')
        minZ = limitTree.GetMinimum('deltaNLL')

        # Create hist
        hname = '{}{}less{}'.format(wcs[0],wcs[1],ceiling)
        if log:
            hname += "_log"
        hist = ROOT.TH3F('hist', hname, len(edge_x)-1, edge_x, len(edge_y)-1, edge_y, len(z)-1, z)
        '''
        for ibinx in range(1,hist.GetNbinsX()-2):
            for ibiny in range(1,hist.GetNbinsX()-2):
                if hist.GetXaxis().GetBinLowEdge(ibinx) - edge_x[ibinx-1] > 0 or hist.GetYaxis().GetBinLowEdge(ibiny) - edge_y[ibiny-1] > 0:
                    print('low diff', hist.GetXaxis().GetBinLowEdge(ibinx) - edge_x[ibinx-1], hist.GetYaxis().GetBinLowEdge(ibiny) - edge_y[ibiny-1])
                    print('low vals', hist.GetXaxis().GetBinLowEdge(ibinx), edge_x[ibinx-1], hist.GetYaxis().GetBinLowEdge(ibiny), edge_y[ibiny-1])
                    print('up diff', hist.GetXaxis().GetBinUpEdge(ibinx) - edge_x[ibinx-1], hist.GetYaxis().GetBinUpEdge(ibiny) - edge_y[ibiny-1])
                    print('up vals', hist.GetXaxis().GetBinUpEdge(ibinx), edge_x[ibinx-1], hist.GetYaxis().GetBinUpEdge(ibiny), edge_y[ibiny-1])
        '''
        # Draw 3D wc[0], wc[1], 2*deltaNLL
        limitTree.Draw('2*(deltaNLL-{}):{}:{}>>+hist'.format(minZ,wcs[0],wcs[1]), '2*(deltaNLL-{})<{}'.format(minZ,ceiling))
        hist.GetYaxis().SetRangeUser(full_edge_x[0], full_edge_x[-1])
        hist.GetZaxis().SetRangeUser(full_edge_y[0], full_edge_y[-1])
        #hist.GetXaxis().SetRangeUser(self.wc_ranges[wcs[0]][0], self.wc_ranges[wcs[0]][1])
        #hist.GetXaxis().SetRangeUser(self.wc_ranges[wcs[1]][0], self.wc_ranges[wcs[1]][1])
        print(wcs[0], self.wc_ranges[wcs[0]][0], self.wc_ranges[wcs[0]][1])
        print(wcs[1], self.wc_ranges[wcs[1]][0], self.wc_ranges[wcs[1]][1])
        hist.GetZaxis().SetRangeUser(0, ceiling)
        # Profile
        hist = hist.Project3DProfile()
        ROOT.gStyle.SetOptStat(0)
        hist.Draw('colz')
        hist.SetTitle(';{};{}'.format(self.texdic[wcs[0].rstrip('i')],self.texdic[wcs[1].rstrip('i')]))

        # Save plot
        canvas.Print(hname+".png",'png')
        canvas.Print(hname+".eps",'eps')
        os.system('ps2pdf -dPDFSETTINGS=/prepress -dEPSCrop {}.eps {}.pdf'.format(hname,hname))

        # Adjust scale so that the best bin has content 0
        #'hist', hname, len(edge_x)-1, edge_x, len(edge_y)-1, edge_y, len(z)-1, z
        #original = hist.Clone('h_conotour')
        #h_contour = hist.Clone('h_contour')
        #h_contour.Clear('ICE')
        #hist = ROOT.TH3F('hist', hname, len(edge_x)-1, edge_x, len(edge_y)-1, edge_y, len(z)-1, z)
        #limitTree.Draw('2*(deltaNLL-{}):{}:{}>>+hist'.format(minZ,wcs[0],wcs[1]), '2*(deltaNLL-{})<{}'.format(minZ,ceiling))
        #hist = hist.Project3DProfile()
        #hist.Draw('colz')

        '''
        hist.SetMaximum(6.8)
        hist.SetContour(2,np.array(6.8))
        hist.GetXaxis().SetRangeUser(edge_x[0], edge_x[-1])
        hist.GetYaxis().SetRangeUser(edge_y[0], edge_y[-1])
        hist.Draw('cont z list')
        canvas.Update()
        conts = ROOT.gROOT.GetListOfSpecials().FindObject("contours")
        print(conts)
        l = conts.At(0)
        c95 = ROOT.TList()
        if conts.GetSize() > 1:
            print("Woah, multiple contours!")
       
        contLevel = conts.At(0)
        for idy in range(0,contLevel.GetSize()):
            gr1=contLevel.At(idy)
            if(gr1.GetN() > 50):
                c95.Add(gr1.Clone())
        #hc95.SetLineColor(ROOT.kCyan-2)
        #hc95.SetLineStyle(6)
        #hc95.SetLineWidth(5)
        hc95 = ROOT.TH2F('hc95', hname, len(edge_x)-1, edge_x, len(edge_y)-1, edge_y)
        hc95.Draw()
        #c95.Draw('P')
        #c68.Draw('L SAME')
        c95.Draw('L SAME')
        #c997.Draw('L SAME')
        '''
        '''
        c68 = self.ContourHelper.GetContour(hist,2.30, list(edge_y), list(edge_x))
        c95 = self.ContourHelper.GetContour(hist,6.18, list(edge_y), list(edge_x))
        c997 = self.ContourHelper.GetContour(hist,11.83, list(edge_y), list(edge_x))
        self.ContourHelper.styleMultiGraph(c68,ROOT.kYellow+1,4,1)
        self.ContourHelper.styleMultiGraph(c95,ROOT.kCyan-2,4,6)
        self.ContourHelper.styleMultiGraph(c997,ROOT.kBlue,4,3)
        #place holders for the legend, since TLine is weird
        '''
        levels = np.array([2.3, 6.18, 11.83])
        hist.SetContour(1, levels)
        hist.Draw('cont1 z')
        hist.GetYaxis().SetRangeUser(full_edge_x[0], full_edge_x[-1])
        hist.GetXaxis().SetRangeUser(full_edge_y[0], full_edge_y[-1])
        canvas.Update()
        '''
        conts = ROOT.gROOT.GetListOfSpecials().FindObject("contours")
        c68 = self.ContourHelper.GetContour(hist,2.30, list(edge_y), list(edge_x))
        hc68 = ROOT.TH1F('c68', 'c68', 1, 0, 1)
        hc95 = ROOT.TH1F('c95', 'c68', 1, 0, 1)
        hc997 = ROOT.TH1F('c997', 'c68', 1, 0, 1)
        hc68.SetLineColor(ROOT.kYellow+1)
        hc95.SetLineColor(ROOT.kCyan-2)
        hc997.SetLineColor(ROOT.kBlue)
        hc68.SetLineStyle(1)
        hc95.SetLineStyle(6)
        hc997.SetLineStyle(3)
        hc68.SetLineWidth(5)
        hc95.SetLineWidth(5)
        hc997.SetLineWidth(5)
        h_contour = ROOT.TH2F('hist', 'h_contour', len(edge_x)-1, edge_x, len(edge_y)-1, edge_y)

        #h_contour.Draw()
        #c68.Draw('L SAME')
        #c95.Draw('L SAME')
        #c997.Draw('L SAME')
        '''
        canvas.SetGrid()
        canvas.Print(hname+"contour.png",'png')
        canvas.Print(hname+"contour.eps",'eps')
        os.system('ps2pdf -dPDFSETTINGS=/prepress -dEPSCrop {}contour.eps {}contour.pdf'.format(hname,hname))

        # Close and delete temporary file
        rootFile.Close()
        os.system('rm ../fit_files/higgsCombine.var{}.MultiDimFit.root'.format(names[0]))

    def ContourPlotEFT(self, name='.test', wcs=[], final=False):
        #hist2d = self.LLPlot2DEFT(name,wcs,9,False)
        #hist = ROOT.TH2F('hist', hname, points, self.wc_ranges[wcs[1]][0], self.wc_ranges[wcs[1]][1], points, self.wc_ranges[wcs[0]][0], self.wc_ranges[wcs[0]][1])
        #limitTree.Draw('2*(deltaNLL-{}):{}:{}>>hist({},{},{},{},{},{})'.format(minZ,wcs[1],wcs[0],points,self.wc_ranges[wcs[0]][0],self.wc_ranges[wcs[0]][1],points,self.wc_ranges[wcs[1]][0],self.wc_ranges[wcs[1]][1]), '2*(deltaNLL-{})<{}'.format(minZ,ceiling), 'prof colz')
        if len(wcs)!=2:
            logging.error("Function 'ContourPlot' requires exactly two wcs!")
            return
        if not os.path.exists('../fit_files/higgsCombine{}.MultiDimFit.root'.format(name)):
            logging.error("File higgsCombine{}.MultiDimFit.root does not exist!".format(name))
            return

        best2DeltaNLL = 1000000
        ROOT.gROOT.SetBatch(True)
        canvas = ROOT.TCanvas('c','c',800,800)

        # Get Grid scan and copy to h_contour
        # wcs[0] is y-axis variable, wcs[1] is x-axis variable
        gridFile = ROOT.TFile.Open('../fit_files/higgsCombine{}.MultiDimFit.root'.format(name))
        gridTree = gridFile.Get('limit')
        minZ = gridTree.GetMinimum('deltaNLL')
        points = 100 
        #gridTree.Draw('2*(deltaNLL-{}):{}:{}>>+hist'.format(minZ,wcs[0],wcs[1]), '2*(deltaNLL-{})<{}'.format(minZ,9))
        #hist2d = ROOT.TH3F('hist', 'hist2d', points, self.wc_ranges[wcs[1]][0], self.wc_ranges[wcs[1]][1], points, self.wc_ranges[wcs[0]][0], self.wc_ranges[wcs[0]][1], 100, 0, 9)
        #hist2d = canvas.GetPrimitive("hist")
        #hist2d = hist2d.Project3DProfile()
        nLL997 = 11.83
        #gridTree.Draw('2*(deltaNLL-{}):{}:{}>>grid(points,{},{},points,{},{})'.format(minZ,wcs[1],wcs[0],self.wc_ranges[wcs[0]][0]*1.1,self.wc_ranges[wcs[0]][1]*1.1,self.wc_ranges[wcs[1]][0]*1.1,self.wc_ranges[wcs[1]][1]*1.1), '2*(deltaNLL-{})<{}'.format(minZ, nLL997*2), 'prof colz')
        # Double the 99.7% CI for better plotting
        gridTree.Draw('2*(deltaNLL-{}):{}:{}>>grid({},{},{},{},{},{})'.format(minZ,wcs[1],wcs[0],points,self.wc_ranges[wcs[0]][0]*1.1,self.wc_ranges[wcs[0]][1]*1.1,points,self.wc_ranges[wcs[1]][0]*1.1,self.wc_ranges[wcs[1]][1]*1.1), '2*(deltaNLL-{})<{}'.format(minZ, nLL997*2), 'prof colz')
        original = ROOT.TProfile2D(canvas.GetPrimitive('grid'))
        h_contour = ROOT.TProfile2D('h_contour','h_contour',points,self.wc_ranges[wcs[1]][0]*1.1,self.wc_ranges[wcs[1]][1]*1.1,points,self.wc_ranges[wcs[0]][0]*1.1,self.wc_ranges[wcs[0]][1]*1.1)
        h_contour = original.Clone('h_conotour')

        canvas.Print('{}{}2D.png'.format(wcs[0],wcs[1]),'png')
        #original.Copy(h_contour)

        # Adjust scale so that the best bin has content 0
        best2DeltaNLL = original.GetMinimum()
        for xbin in range(original.GetNbinsX()):
            xcoord = original.GetXaxis().GetBinCenter(xbin)
            for ybin in range(original.GetNbinsY()):
                ycoord = original.GetYaxis().GetBinCenter(ybin)
                if original.GetBinContent(1+xbin,1+ybin)==0:
                    h_contour.Fill(xcoord,ycoord,1000)
                if original.GetBinContent(1+xbin,1+ybin)!=0:
                    h_contour.Fill(xcoord,ycoord,original.GetBinContent(1+xbin,1+ybin)-best2DeltaNLL)
                #h_contour.SetBinContent(1+xbin,1+ybin,original.GetBinContent(1+xbin,1+ybin)-best2DeltaNLL)


        # Exclude data outside of the contours
        #h_contour.SetMaximum(11.83)
        #h_contour.SetContour(200)
        #h_contour.GetZaxis().SetRangeUser(0,21);
        h_contour.GetXaxis().SetRange(1,h_contour.GetNbinsX()-3)
        h_contour.GetYaxis().SetRange(1,h_contour.GetNbinsY()-3)
        #h_contour.GetXaxis().SetRangeUser(self.wc_ranges[wcs[1]][0]*1.1,self.wc_ranges[wcs[1]][1]*1.1)
        #h_contour.GetYaxis().SetRangeUser(self.wc_ranges[wcs[0]][0]*1.1,self.wc_ranges[wcs[0]][1]*1.1)

        # Set Contours
        c68 = self.ContourHelper.GetContour(h_contour,2.30)
        c95 = self.ContourHelper.GetContour(h_contour,6.18)
        c997 = self.ContourHelper.GetContour(h_contour,11.83)
        c681D = self.ContourHelper.GetContour(h_contour,1.00)
        c951D = self.ContourHelper.GetContour(h_contour,4.00)
        c9971D = self.ContourHelper.GetContour(h_contour,9.00)

 
        self.ContourHelper.styleMultiGraph(c68,ROOT.kYellow+1,4,1)
        self.ContourHelper.styleMultiGraph(c95,ROOT.kCyan-2,4,6)
        self.ContourHelper.styleMultiGraph(c997,ROOT.kBlue,4,3)


        #place holders for the legend, since TLine is weird
        hc68 = ROOT.TH1F('c68', 'c68', 1, 0, 1)
        hc95 = ROOT.TH1F('c95', 'c68', 1, 0, 1)
        hc997 = ROOT.TH1F('c997', 'c68', 1, 0, 1)
        hc68.SetLineColor(ROOT.kYellow+1)
        hc95.SetLineColor(ROOT.kCyan-2)
        hc997.SetLineColor(ROOT.kBlue)
        hc68.SetLineStyle(1)
        hc95.SetLineStyle(6)
        hc997.SetLineStyle(3)
        hc68.SetLineWidth(5)
        hc95.SetLineWidth(5)
        hc997.SetLineWidth(5)
        self.ContourHelper.styleMultiGraph(c681D,ROOT.kYellow+1,1,3)
        self.ContourHelper.styleMultiGraph(c951D,ROOT.kCyan-2,1,3)
        self.ContourHelper.styleMultiGraph(c9971D,ROOT.kBlue,1,3)

        # Marker for SM point
        marker_1 = ROOT.TMarker()
        marker_1.SetMarkerSize(3.0)
        marker_1.SetMarkerColor(97)
        marker_1.SetMarkerStyle(33)
        #marker_2 = ROOT.TMarker()
        #marker_2.SetMarkerSize(1.8)
        #marker_2.SetMarkerColor(89)
        #marker_2.SetMarkerStyle(33)
        hSM = ROOT.TH1F('SM', 'SM', 1, 0, 1)
        hSM.SetMarkerStyle(33)
        hSM.SetMarkerColor(97)
        hSM.SetMarkerSize(2)
 
        # Change format of plot
        h_contour.SetStats(0)
        #h_contour.SetTitle("Significance Contours")
        h_contour.SetTitle("")
        h_contour.GetYaxis().SetTitle(self.texdic[wcs[1].rstrip('i')])
        h_contour.GetXaxis().SetTitle(self.texdic[wcs[0].rstrip('i')])

        # CMS-required text
        self.CMS_text = ROOT.TLatex(0.1, 0.945, "CMS")# Simulation")
        self.CMS_text.SetNDC(1)
        self.CMS_text.SetTextSize(0.04)
        self.CMS_text.SetTextAlign(13)
        self.CMS_text.Draw('same')
        if not final: self.CMS_extra = ROOT.TLatex(0.2, 0.945, "Preliminary")# Simulation")
        #self.CMS_extra = ROOT.TLatex(0.2, 0.945, "Supplementary")# Simulation")
        self.CMS_extra.SetNDC(1)
        self.CMS_extra.SetTextSize(0.04)
        self.CMS_extra.SetTextAlign(13)
        self.CMS_extra.SetTextFont(52)
        if not final: self.CMS_extra.Draw('same')
        scan_name = 'Other WCs profiled'
        if 'Froz' in name or 'Freeze' in name or 'frozen' in name:
            scan_name = 'Other WCs fixed to SM'
        self.scan_type = ROOT.TLatex(0.15, 0.885, scan_name)
        self.scan_type.SetNDC(1)
        self.scan_type.SetTextSize(0.04)
        self.scan_type.SetTextAlign(13)
        self.scan_type.SetTextFont(42)
        self.scan_type.Draw('same')
        self.Lumi_text = ROOT.TLatex(0.9, 0.91, self.luminosity_text)
        self.Lumi_text.SetNDC(1)
        self.Lumi_text.SetTextSize(0.04)
        self.Lumi_text.SetTextAlign(30)
        self.Lumi_text.SetTextFont(42)
        self.Lumi_text.Draw('same')

        # Draw and save plot
        h_contour.GetXaxis().SetTitleOffset(1.1)
        h_contour.GetXaxis().SetTitleSize(0.04)
        h_contour.GetXaxis().SetLabelSize(0.04)
        h_contour.GetYaxis().SetTitleOffset(1.1)
        h_contour.GetYaxis().SetTitleSize(0.04)
        h_contour.GetXaxis().SetLabelSize(0.04)
        #h_contour.GetYaxis().SetNdivisions(7)
        #hist2d.Draw('colz')
        #h_contour.Draw('same')

        h_contour.Draw('AXIS')

        #canvas.Print('contour.png','png')

        c68.Draw('L SAME')
        c95.Draw('L SAME')
        c997.Draw('L SAME')

        marker_1.DrawMarker(0,0)
        #marker_2.DrawMarker(0,0)

        #c = [2.3, 6.18, 11.83]
        #original.SetContourLevel(0, c[0])
        #original.SetContourLevel(1, c[1])
        #original.SetContourLevel(2, c[2])
        #import numpy
        #original.SetContour(3, numpy.array(c))
        #original.SetMaximum(3)
        #ROOT.gStyle.SetOptStat(0)
        #original.Draw("cont1z")
        #marker_1.DrawMarker(0,0)
        #marker_2.DrawMarker(0,0)


        self.CMS_text.Draw('same')
        if not final: self.CMS_extra.Draw('same')
        self.scan_type.Draw('same')
        self.Lumi_text.Draw('same')
        canvas.SetGrid()
        if final: canvas.Print('{}{}contour_final.png'.format(wcs[0],wcs[1]),'png')
        else:
            canvas.Print('{}{}contour.png'.format(wcs[0],wcs[1]),'png')
            canvas.Print('{}{}contour.eps'.format(wcs[0],wcs[1]),'eps')
            os.system('sed -i "s/STIXGeneral-Italic/STIXXGeneral-Italic/g" {}{}contour.eps'.format(wcs[0],wcs[1],wcs[0],wcs[1]))
            os.system('ps2pdf -dPDFSETTINGS=/prepress -dEPSCrop {}{}contour.eps {}{}contour.pdf'.format(wcs[0],wcs[1],wcs[0],wcs[1]))
        if final: 
            #canvas.Print('{}{}contour_final.pdf'.format(wcs[0],wcs[1]),'pdf')
            canvas.Print('{}{}contour_final.png'.format(wcs[0],wcs[1]),'png')
            canvas.Print('{}{}contour_final.eps'.format(wcs[0],wcs[1]),'eps')
            #convert EPS to PDF to preserve \ell
            os.system('sed -i "s/STIXGeneral-Italic/STIXXGeneral-Italic/g" {}{}contour_final.eps'.format(wcs[0],wcs[1],wcs[0],wcs[1]))
            os.system('ps2pdf -dPDFSETTINGS=/prepress -dEPSCrop {}{}contour_final.eps {}{}contour_final.pdf'.format(wcs[0],wcs[1],wcs[0],wcs[1]))
        else: 
            canvas.Print('{}{}contour.pdf'.format(wcs[0],wcs[1]),'pdf')
            canvas.Print('{}{}contour_prelim.png'.format(wcs[0],wcs[1]),'png')
            canvas.Print('{}{}contour_prelim.eps'.format(wcs[0],wcs[1]),'eps')
            os.system('sed -i "s/STIXGeneral-Italic/STIXXGeneral-Italic/g" {}{}contour_prelim.eps'.format(wcs[0],wcs[1],wcs[0],wcs[1]))
            os.system('ps2pdf -dPDFSETTINGS=/prepress -dEPSCrop {}{}contour_prelim.eps {}{}contour_prelim.pdf'.format(wcs[0],wcs[1],wcs[0],wcs[1]))

        # Versions with 1D lines included
        c681D.Draw('L SAME')
        c951D.Draw('L SAME')
        c9971D.Draw('L SAME')
        if final: canvas.Print('{}{}contour_final_1d.png'.format(wcs[0],wcs[1]),'png')
        else:
            canvas.Print('{}{}contour_1d.png'.format(wcs[0],wcs[1]),'png')
            canvas.Print('{}{}contour_1d.eps'.format(wcs[0],wcs[1]),'eps')
            os.system('sed -i "s/STIXGeneral-Italic/STIXXGeneral-Italic/g" {}{}contour_1d.eps'.format(wcs[0],wcs[1],wcs[0],wcs[1]))
            os.system('ps2pdf -dPDFSETTINGS=/prepress -dEPSCrop {}{}contour_1d.eps {}{}contour_1d.pdf'.format(wcs[0],wcs[1],wcs[0],wcs[1]))
        if final: 
            #canvas.Print('{}{}contour_final_1d.pdf'.format(wcs[0],wcs[1]),'pdf')
            canvas.Print('{}{}contour_final_1d.png'.format(wcs[0],wcs[1]),'png')
            canvas.Print('{}{}contour_final_1d.eps'.format(wcs[0],wcs[1]),'eps')
            #convert EPS to PDF to preserve \ell
            os.system('sed -i "s/STIXGeneral-Italic/STIXXGeneral-Italic/g" {}{}contour_final_1d.eps'.format(wcs[0],wcs[1],wcs[0],wcs[1]))
            os.system('ps2pdf -dPDFSETTINGS=/prepress -dEPSCrop {}{}contour_final_1d.eps {}{}contour_final_1d.pdf'.format(wcs[0],wcs[1],wcs[0],wcs[1]))
        else: 
            canvas.Print('{}{}contour_1d.pdf'.format(wcs[0],wcs[1]),'pdf')
            canvas.Print('{}{}contour_prelim_1d.png'.format(wcs[0],wcs[1]),'png')
            canvas.Print('{}{}contour_prelim_1d.eps'.format(wcs[0],wcs[1]),'eps')
            os.system('sed -i "s/STIXGeneral-Italic/STIXXGeneral-Italic/g" {}{}contour_prelim_1d.eps'.format(wcs[0],wcs[1],wcs[0],wcs[1]))
            os.system('ps2pdf -dPDFSETTINGS=/prepress -dEPSCrop {}{}contour_prelim_1d.eps {}{}contour_prelim_1d.pdf'.format(wcs[0],wcs[1],wcs[0],wcs[1]))
        canvas = ROOT.TCanvas('cleg', 'cleg', 600, 100)
        hSM.SetMarkerSize(2)
        hc68.SetMarkerSize(2)
        hc95.SetMarkerSize(2)
        hc997.SetMarkerSize(2)
        canvas.cd()
        legend = ROOT.TLegend(0.12,0.7,0.3,0.895)
        legend = ROOT.TLegend(0.01,0.01,0.99,0.99)
        legend = ROOT.TLegend(0.01,0.01,0.90,0.90)
        # Bob Cousins stated 2+D should always be percentages, since e.g. "1 sigma" is not actually 68 for a 2D contour
        # https://hypernews.cern.ch/HyperNews/CMS/get/statistics/764/1.html
        legend.AddEntry(hc68,"68.3% CI",'l')
        legend.AddEntry(hc95,"95.5% CI",'l')
        legend.AddEntry(hc997,"99.7% CI",'l')
        legend.AddEntry(hSM,"SM value",'p')
        legend.SetTextSize(0.3)
        #legend.SetTextSize(0.025)
        legend.SetNColumns(4)
        legend.Draw()
        canvas.Print('contour_leg.png')
        canvas.Print('contour_leg.eps')
        canvas.Print('contour_leg.pdf')
        os.system('sed -i "s/STIXGeneral-Italic/STIXXGeneral-Italic/g" contour_leg.eps')
        os.system('ps2pdf -dPDFSETTINGS=/prepress -dEPSCrop contour_leg.eps contour_leg.pdf')

        # Save contour to histogram file
        outfile = ROOT.TFile(self.histosFileName,'UPDATE')
        h_contour.Write()
        outfile.Close()

        ROOT.gStyle.SetPalette(57)
        
    def LLPlot1DSM(self, name='.test', param='', log=False):
        if not param:
            logging.error("No parameter specified!")
            return
        if not os.path.exists('../fit_files/higgsCombine{}.MultiDimFit.root'.format(name)):
            logging.error("File higgsCombine{}.MultiDimFit.root does not exist!".format(name))
            return

        ROOT.gROOT.SetBatch(True)
        canvas = ROOT.TCanvas('c','c',800,800)

        # Get scan tree
        rootFile = ROOT.TFile.Open('../fit_files/higgsCombine{}.MultiDimFit.root'.format(name))
        limitTree = rootFile.Get('limit')

        # Get coordinates for TGraph
        graphparamvals = []
        graphnlls = []
        for entry in range(limitTree.GetEntries()):
            limitTree.GetEntry(entry)
            graphparamvals.append(limitTree.GetLeaf(param).GetValue(0))
            graphnlls.append(2*limitTree.GetLeaf('deltaNLL').GetValue(0))

        # Rezero the y axis and make the tgraphs
        graphnlls = [val-min(graphnlls) for val in graphnlls]
        graph = ROOT.TGraph(len(graphparamvals),numpy.asarray(graphparamvals),numpy.asarray(graphnlls))
        graph.Draw("AP")
        del graphnlls,graphparamvals

        # Squeeze X down to whatever range captures the float points
        xmin = limitTree.GetMinimum(param)
        xmax = limitTree.GetMaximum(param)
        #for idx in range(graph.GetN()):
        #    if graph.GetY()[idx] < 10 and graph.GetX()[idx] < xmin:
        #        xmin = graph.GetX()[idx]
        #    if graph.GetY()[idx] < 10 and graph.GetX()[idx] > xmax:
        #        xmax = graph.GetX()[idx]
        graph.GetXaxis().SetRangeUser(xmin,xmax)
        graph.GetYaxis().SetRangeUser(-0.1,10)

        #Change markers from invisible dots to nice triangles
        graph.SetTitle("")
        graph.SetMarkerStyle(26)
        graph.SetMarkerSize(1)
        graph.SetMinimum(-0.1)

        #Add 1-sigma and 2-sigma lines. (Vertical lines were too hard, sadly)
        canvas.SetGrid(1)

        line68 = ROOT.TLine(xmin,1,xmax,1)
        line68.Draw('same')
        line68.SetLineColor(ROOT.kYellow+1)
        line68.SetLineWidth(3)
        line68.SetLineStyle(7)

        line95 = ROOT.TLine(xmin,4,xmax,4)
        line95.Draw('same')
        line95.SetLineColor(ROOT.kCyan-2)
        line95.SetLineWidth(3)
        line95.SetLineStyle(7)

        # Labels
        Title = ROOT.TLatex(0.5, 0.95, "{} 2#DeltaNLL".format(param))
        Title.SetNDC(1)
        Title.SetTextAlign(20)
        Title.Draw('same')
        graph.GetXaxis().SetTitle(param)

        # CMS-required text
        self.CMS_text = ROOT.TLatex(0.665, 0.93, "CMS")# Simulation")
        self.CMS_text.SetNDC(1)
        self.CMS_text.SetTextSize(0.02)
        self.CMS_text.SetTextAlign(30)
        self.CMS_text.Draw('same')
        if not final: self.CMS_extra = ROOT.TLatex(0.9, 0.90, "Preliminary")# Simulation")
        self.CMS_extra.SetNDC(1)
        self.CMS_extra.SetTextSize(0.02)
        self.CMS_extra.SetTextAlign(30)
        self.CMS_extra.SetTextFont(52)
        self.CMS_extra.Draw('same')
        self.Lumi_text = ROOT.TLatex(0.7, 0.91, self.luminosity_text)
        self.Lumi_text.SetNDC(1)
        self.Lumi_text.SetTextSize(0.02)
        self.Lumi_text.SetTextAlign(30)
        self.Lumi_text.SetTextFont(42)
        self.Lumi_text.Draw('same')

        #Check log option, then save as image
        if log:
            graph.SetMinimum(0.1)
            graph.SetLogz()
            canvas.Print('{}{}_1DNLL_log.png'.format(param,name),'png')
        else:
            canvas.Print('{}{}_1DNLL.png'.format(param,name),'png')

        rootFile.Close()
        
    def BatchLLPlot1DSM(self, basename='.test', frozen=False, scan_params=[], log=False):
        if not scan_params:
            scan_params = self.SMMus

        ROOT.gROOT.SetBatch(True)

        for param in scan_params:
            self.LLPlot1DSM(basename+'.'+param, param, log)

    def LLPlot2DSM(self, name='.test', params=[], ceiling=1, log=False):
        if len(params)!=2:
            logging.error("Function 'LLPlot2D' requires exactly two parameters!")
            return
        if not os.path.exists('../fit_files/higgsCombine{}.MultiDimFit.root'.format(name)):
            logging.error("File higgsCombine{}.MultiDimFit.root does not exist!".format(name))
            return

        ROOT.gROOT.SetBatch(True)

        canvas = ROOT.TCanvas('c','c',800,800)

        # Open file and draw 2D histogram
        rootFile = ROOT.TFile.Open('../fit_files/higgsCombine{}.MultiDimFit.root'.format(name))
        limitTree = rootFile.Get('limit')
        hname = '{}{}_{}_less{}'.format(params[0],params[1],name,ceiling)
        if log:
            hname += "_log"
        minZ = limitTree.GetMinimum('deltaNLL')
        ymin = limitTree.GetMinimum(params[0])
        ymax = limitTree.GetMaximum(params[0])
        xmin = limitTree.GetMinimum(params[1])
        xmax = limitTree.GetMaximum(params[1])

        #limitTree.Draw('2*(deltaNLL-{}):{}:{}>>{}(200,0,15,200,0,15)'.format(minZ,params[0],params[1],hname), '2*deltaNLL<{}'.format(ceiling), 'prof colz')
        limitTree.Draw('2*(deltaNLL-{}):{}:{}>>{}(200,{},{},200,{},{})'.format(minZ,params[0],params[1],hname,xmin,xmax,ymin,ymax), '2*deltaNLL<{}'.format(ceiling), 'prof colz')        

        hist = canvas.GetPrimitive(hname)

        # Draw best fit point from grid scan
        #limit.Draw(params[0]+":"+params[1],'quantileExpected==-1','p same') # Best fit point from grid scan
        #best_fit = canvas.FindObject('Graph')
        #best_fit.SetMarkerSize(1)
        #best_fit.SetMarkerStyle(34)
        #best_fit.Draw("p same")

        # Change plot formats
        #hist.GetXaxis().SetRangeUser(0,5)
        #hist.GetYaxis().SetRangeUser(0,3)
        #hist.GetXaxis().SetRangeUser(-5,5)
        #hist.GetYaxis().SetRangeUser(-5,5)
        if log:
            canvas.SetLogz()
        hist.GetYaxis().SetTitle(params[0])
        hist.GetXaxis().SetTitle(params[1])
        hist.SetTitle("2*deltaNLL < {}".format(ceiling))
        hist.SetStats(0)

        # CMS-required text
        self.CMS_text = ROOT.TLatex(0.665, 0.93, "CMS")# Simulation")
        self.CMS_text.SetNDC(1)
        self.CMS_text.SetTextSize(0.02)
        self.CMS_text.SetTextAlign(30)
        self.CMS_text.Draw('same')
        if not final: self.CMS_extra = ROOT.TLatex(0.9, 0.90, "Preliminary")# Simulation")
        self.CMS_extra.SetNDC(1)
        self.CMS_extra.SetTextSize(0.02)
        self.CMS_extra.SetTextAlign(30)
        self.CMS_extra.SetTextFont(52)
        self.CMS_extra.Draw('same')
        self.Lumi_text = ROOT.TLatex(0.7, 0.91, self.luminosity_text)
        self.Lumi_text.SetNDC(1)
        self.Lumi_text.SetTextSize(0.02)
        self.Lumi_text.SetTextAlign(30)
        self.Lumi_text.SetTextFont(42)
        self.Lumi_text.Draw('same')

        # Save plot
        canvas.Print(hname+".png",'png')

        # Save to root file
        # Log settings don't save to the histogram, so redundant to save those
        if not log:
            outfile = ROOT.TFile(self.histosFileName,'UPDATE')
            hist.Write()
            outfile.Close()

    def ContourPlotSM(self, name='.test', params=[]):
        if len(params)!=2:
            logging.error("Function 'ContourPlot' requires exactly two parameters!")
            return
            
        best2DeltaNLL = 1000000
        ROOT.gROOT.SetBatch(True)
        canvas = ROOT.TCanvas('c','c',800,800)

        # Get Grid scan and copy to h_contour
        # params[0] is y-axis variable, params[1] is x-axis variable
        gridFile = ROOT.TFile.Open('../fit_files/higgsCombine{}.MultiDimFit.root'.format(name))
        gridTree = gridFile.Get('limit')
        minZ = gridTree.GetMinimum('deltaNLL')
        #gridTree.Draw('2*(deltaNLL-{}):{}:{}>>grid(200,0,15,200,0,15)'.format(minZ,params[0],params[1]), '', 'prof colz')
        gridTree.Draw('2*(deltaNLL-{}):{}:{}>>grid(100,{},{},100,{},{})'.format(minZ,params[0],params[1],self.sm_ranges[params[1]][0],self.sm_ranges[params[1]][1],self.sm_ranges[params[0]][0],self.sm_ranges[params[0]][1]), '', 'prof colz')
        #gridTree.Draw('2*deltaNLL:{}:{}>>grid(50,0,30,50,0,30)'.format(params[0],params[1]), '', 'prof colz')
        original = ROOT.TProfile2D(canvas.GetPrimitive('grid'))
        h_contour = ROOT.TProfile2D('h_contour','h_contour',100,self.sm_ranges[params[1]][0],self.sm_ranges[params[1]][1],100,self.sm_ranges[params[0]][0],self.sm_ranges[params[0]][1])

        # Adjust scale so that the best bin has content 0
        best2DeltaNLL = original.GetMinimum()
        for xbin in range(original.GetNbinsX()):
            xcoord = original.GetXaxis().GetBinCenter(xbin)
            for ybin in range(original.GetNbinsY()):
                ycoord = original.GetYaxis().GetBinCenter(ybin)
                if original.GetBinContent(1+xbin,1+ybin)==0:
                    h_contour.Fill(xcoord,ycoord,1000)
                if original.GetBinContent(1+xbin,1+ybin)!=0:
                    h_contour.Fill(xcoord,ycoord,original.GetBinContent(1+xbin,1+ybin)-best2DeltaNLL)
                #h_contour.SetBinContent(1+xbin,1+ybin,original.GetBinContent(1+xbin,1+ybin)-best2DeltaNLL)

        # Exclude data outside of the contours
        #h_contour.SetMaximum(11.83)
        #h_contour.SetContour(200)
        #h_contour.GetZaxis().SetRangeUser(0,21);
        #h_contour.GetXaxis().SetRangeUser(0,6); # ttH
        #h_contour.GetXaxis().SetRangeUser(0,4); # tllq
        #h_contour.GetYaxis().SetRangeUser(0,3); # tll, tllnu
        h_contour.GetXaxis().SetRange(1,h_contour.GetNbinsX()-3)
        h_contour.GetYaxis().SetRange(1,h_contour.GetNbinsY()-3)

        # Set Contours
        c68 = self.ContourHelper.GetContour(h_contour,2.30)
        c95 = self.ContourHelper.GetContour(h_contour,6.18)
        c997 = self.ContourHelper.GetContour(h_contour,11.83)
        self.ContourHelper.styleMultiGraph(c68,ROOT.kYellow+1,3,1)
        self.ContourHelper.styleMultiGraph(c95,ROOT.kCyan-2,3,1)
        self.ContourHelper.styleMultiGraph(c997,ROOT.kGreen-2,3,1)

        # Marker for SM point
        marker_1 = ROOT.TMarker()
        marker_1.SetMarkerSize(2.0)
        marker_1.SetMarkerColor(97)
        marker_1.SetMarkerStyle(33)
#        marker_2 = ROOT.TMarker()
#        marker_2.SetMarkerSize(1.2)
#        marker_2.SetMarkerColor(89)
#        marker_2.SetMarkerStyle(33)
        
        # Misc Markers -- use as needed
        # Simultaneous Fit Marker -- use as needed
        #simulFit = ROOT.TMarker(0.96,1.11,20) # tllq,ttll
        simulFit = ROOT.TMarker(2.58,0.70,20) # ttH,ttlnu
        # Central Fit Marker -- use as needed
        centralFit = ROOT.TGraphAsymmErrors(1)
        #centralFit.SetPoint(0,0.58,1.24) # tllq,ttll
        #centralFit.SetPointError(0,0.54,0.61,0.24,0.31) # tllq,ttll
        centralFit.SetPoint(0,2.56,0.84) # ttH,ttlnu
        centralFit.SetPointError(0,0.72,0.87,0.36,0.43) # ttH,ttlnu
        centralFit.SetMarkerSize(2)
        centralFit.SetMarkerStyle(6)
        centralFit.SetLineColor(2)
        # Dedicated Fit Marker -- use as needed
        dedicatedFit = ROOT.TGraphAsymmErrors(1)
        #dedicatedFit.SetPoint(0,1.01,1.28) # tZq,ttZ
        #dedicatedFit.SetPointError(0,0.21,0.23,0.13,0.14) # tZq,ttZ
        dedicatedFit.SetPoint(0,0.75,1.23) # ttH,ttW
        dedicatedFit.SetPointError(0,0.43,0.46,0.28,0.31) # ttH,ttW
        dedicatedFit.SetMarkerSize(2)
        dedicatedFit.SetMarkerStyle(6)
        dedicatedFit.SetLineColor(8)

        # Change format of plot
        h_contour.SetStats(0)
        h_contour.SetTitle("Significance Contours")
        h_contour.GetYaxis().SetTitle(params[0])
        h_contour.GetXaxis().SetTitle(params[1])

        # CMS-required text
        self.CMS_text = ROOT.TLatex(0.9, 0.93, "CMS")# Simulation")
        self.CMS_text.SetNDC(1)
        self.CMS_text.SetTextSize(0.02)
        self.CMS_text.SetTextAlign(30)
        self.CMS_text.Draw('same')
        if not final: self.CMS_extra = ROOT.TLatex(0.9, 0.90, "Preliminary")# Simulation")
        self.CMS_extra.SetNDC(1)
        self.CMS_extra.SetTextSize(0.02)
        self.CMS_extra.SetTextAlign(30)
        self.CMS_extra.SetTextFont(52)
        self.CMS_extra.Draw('same')
        self.Lumi_text = ROOT.TLatex(0.01, 0.91, self.luminosity_text)
        self.Lumi_text.SetNDC(1)
        self.Lumi_text.SetTextSize(0.02)
        self.Lumi_text.SetTextAlign(30)
        self.Lumi_text.SetTextFont(42)
        self.Lumi_text.Draw('same')

        # Draw and save plot
#        h_contour.Draw('AXIS')
        h_contour.Draw("COLZ CONT LIST")
        gPad.Update()

        c68.Draw('L SAME')
        c95.Draw('L SAME')
        c997.Draw('L SAME')
        #marker_1.DrawMarker(1,1)
        #marker_2.DrawMarker(1,1)
        simulFit.Draw('same')
        centralFit.Draw('same')
        dedicatedFit.Draw('same')

        self.CMS_text.Draw('same')
        if not final: self.CMS_extra.Draw('same')
        self.Lumi_text.Draw('same')
        if final: canvas.Print('{}{}contour_final.png'.format(params[0],params[1]),'png')
        else: canvas.Print('{}{}contour.png'.format(params[0],params[1]),'png')

        # Save contour to histogram file
        outfile = ROOT.TFile(self.histosFileName,'UPDATE')
        h_contour.Write()
        outfile.Close()

        ROOT.gStyle.SetPalette(57)

    def CorrelationMatrix(self, name='', nuisances=False, SMfit=True, freeze=False, abs_min=0.):

        ROOT.gROOT.SetBatch(True)
        canvas = ROOT.TCanvas()

        # Get rooFit object
        rooFitFile = ROOT.TFile.Open('../fit_files/multidimfit{}.root'.format(name))
        rooFit = rooFitFile.Get('fit_mdf')

        # Get correlation matrix
        rooFit.correlationHist().Draw('colz')
        matrix = canvas.GetPrimitive('correlation_matrix')

        # Find pairs above abs_min
        corr = []
        cov = []
        m = []
        wcs = []
        mwcs = []
        for ibinx in range(1, matrix.GetXaxis().GetNbins()):
            for ibiny in range(1, matrix.GetYaxis().GetNbins()):
                val = matrix.GetBinContent(ibinx, ibiny)
                cx = matrix.GetXaxis().GetBinLabel(ibinx)
                cy = matrix.GetYaxis().GetBinLabel(ibiny)
                if cx[0] != 'c' or cy[0] != 'c': continue
                if 'charge' in cx or 'charge' in cy: continue
                if cx not in wcs: wcs.append(cx)
                ex = rooFit.floatParsFinal().find(cx).getError()
                ey = rooFit.floatParsFinal().find(cy).getError()
                m.append(val/(ex*ey))
                print('x=', cx, 'y=', cy, 'corr=', val, 'sigma_x=', ex, 'sigma_y=', ey, 'cov=', val/(ex*ey))
                if abs(val) > abs_min:
                    # skip e.g. (ctG, cpQM) <-> (cpQM, ctG)
                    if any(cx in c and cy in c for c in corr): continue
                    # skip e.g. (ctG, ctG)
                    if cx == cy: continue
                    corr.append((cx, cy))#, val))

        # Eigenvalue/eigenvector stuff
        nwcs = len(wcs)
        m = np.array(m).reshape(nwcs,nwcs)
        print(m)
        eigenval, eigenvec = np.linalg.eig(m)
        print('Eigenvalues=', eigenval)
        for n,eig in enumerate(eigenvec.T):
            mask = np.abs(eig)>abs_min
            if not any(mask): continue
            print('Eigenvector for', eigenval[n])
            print('    wcs          ', [wcs[x] for x in np.argwhere(mask).T[0]])
            print('    |basis| >', abs_min, eig[mask])


        print('Interesting pairs: ', corr)

        # Decide whether or not to keep the nuisance parameters in
        # If not, the number of bins (parameters) varies on whether the scan froze the others
        if nuisances:
            matrix.SetName("corrMatrix")
        else:
            if SMfit:
                SMmu = ['mu_ttH','mu_ttlnu','mu_ttll','mu_tllq']
                muBinsX, muBinsY = [], []
                for idx,label in enumerate(matrix.GetXaxis().GetLabels()):
                    if label in SMmu: muBinsX.append(1+idx)
                for idy,label in enumerate(matrix.GetYaxis().GetLabels()):
                    if label in SMmu: muBinsY.append(1+idy)
                newmatrix = ROOT.TH2D("Correlation Matrix","Correlation Matrix",4,0,4,4,0,4)
                for idx,binx in enumerate(muBinsX):
                    for idy,biny in enumerate(muBinsY):
                        newmatrix.SetBinContent(1+idx,4-idy,matrix.GetBinContent(binx,matrix.GetNbinsY()-biny+1))
                    newmatrix.GetXaxis().SetBinLabel(1+idx,matrix.GetXaxis().GetBinLabel(binx))
                for idy,biny in enumerate(muBinsY):
                    newmatrix.GetYaxis().SetBinLabel(4-idy,matrix.GetYaxis().GetBinLabel(matrix.GetNbinsY()-biny+1))

                # Change format of plot
                newmatrix.SetMaximum(1)
                newmatrix.SetMinimum(-1.)
                newmatrix.SetStats(0)
                newmatrix.SetName("corrMatrixSM")
                newmatrix.SetTitle("Correlation Matrix")

                canvas.Clear()
                newmatrix.Draw('colz')
                ROOT.gStyle.SetPaintTextFormat('.2f')

                # Save the plot
                canvas.Print(newmatrix.GetName()+'.png','png')
                newmatrix.Draw('same text')
                canvas.Print(newmatrix.GetName()+'text.png','png')

                # Save the plot to the histogram file
                outfile = ROOT.TFile(self.histosFileName,'UPDATE')
                newmatrix.Write()
                outfile.Close()
                    
            else:
                nbinsx = matrix.GetNbinsX()
                nbinsy = matrix.GetNbinsY()
                orig = matrix.Clone('orig')
                matrix = ROOT.TH2F("corrMatrix_noNuisances", "corrMatrix_noNuisances", nwcs, 0, nwcs, nwcs, 0, nwcs)
                for ibinx in range(1, nbinsx+1):
                    for ibiny in range(1, nbinsy+1):
                        val = orig.GetBinContent(ibinx, ibiny)
                        cx = orig.GetXaxis().GetBinLabel(ibinx)
                        cy = orig.GetYaxis().GetBinLabel(ibiny)
                        if cx[0] != 'c' or cy[0] != 'c': continue
                        if 'charge' in cx or 'charge' in cy: continue
                        matrix.SetBinContent(matrix.FindBin(wcs.index(cx)+0, wcs.index(cy)+0), val)
                        matrix.GetXaxis().SetBinLabel(wcs.index(cx)+1, cx)
                        matrix.GetYaxis().SetBinLabel(wcs.index(cy)+1, cy)
                matrix.GetXaxis().LabelsOption('v')

                # Change format of plot
                matrix.SetStats(0)
                matrix.SetTitle("Correlation Matrix")

                # Save the plot
                canvas.Clear()
                matrix.Draw('colz')
                ROOT.gStyle.SetPaintTextFormat('.2f')

                # Save the plot
                canvas.Print(matrix.GetName()+'.png','png')
                canvas.Print(matrix.GetName()+'.eps','eps')
                os.system('ps2pdf -dPDFSETTINGS=/prepress -dEPSCrop {}.eps {}.pdf'.format(matrix.GetName(), matrix.GetName()))
                matrix.Draw('same text')
                canvas.Print(matrix.GetName()+'text.png','png')
                canvas.Print(matrix.GetName()+'text.eps','eps')
                os.system('ps2pdf -dPDFSETTINGS=/prepress -dEPSCrop {}text.eps {}text.pdf'.format(matrix.GetName(), matrix.GetName()))

                # Save the plot to the histogram file
                outfile = ROOT.TFile(self.histosFileName,'UPDATE')
                matrix.Write()
                outfile.Close()

    def grid2DWC(self, name='', wc='', fits=[]):

        ROOT.gROOT.SetBatch(True)
        canvas = ROOT.TCanvas()

        # Get limit tree
        fit_file = ROOT.TFile.Open('../fit_files/higgsCombine{}.{}.MultiDimFit.root'.format(name,wc))
        limit_tree = fit_file.Get('limit')

        def isclose(a, b, rel_tol=1e-09, abs_tol=0.0):
            return abs(a-b) <= max(rel_tol * max(abs(a), abs(b)), abs_tol)

        # Fined event with minimum NLL
        min_val = 0
        wc_values = []
        for entry in range(limit_tree.GetEntries()):
            limit_tree.GetEntry(entry)
            #min_values.append((entry,2*limit_tree.GetLeaf('deltaNLL').GetValue(0)))
            if limit_tree.GetLeaf(wc).GetValue(0) == fits[wc]: min_val = entry

        # Load other 15 WCs in the minimum entry
        best_vals = []
        for w in self.wcs:
            limit_tree.GetEntry(min_val)
            if w == wc:
                best_vals.append([w, str(limit_tree.GetLeaf(w).GetValue(0))])
            else:
                best_vals.append([w, str(limit_tree.GetLeaf('trackedParam_' + w).GetValue(0))])

        # Close files
        fit_file.Close()
        return best_vals

    def batchGrid2DWC(self, name=['']):
        best = []

        fits_float = self.getIntervalFits(basename_lst=name)
        if any(len(row['scan'].minima) != 1 or row['scan'].minimum_status == 'MINIMUM_UNRESOLVED'
               for row in fits_float):
            raise ValueError("grid2DWC requires one resolved supplied-data minimum per WC")
        fits = {row['scan'].wc: row['scan'].minima[0] for row in fits_float}

        for wc in self.wcs:
            best.append(self.grid2DWC(name, wc, fits))

        j=0
        for i in range(0, len(self.wcs)):
            if i ==0: print('  '.join(self.wcs))
            lst = [str(round(float(w[1]), 3)) for w in best[i]]
            print(best[i][j][0], ' '.join(lst))
            j = j + 1

    def Batch2DPlotsEFT(self, gridScanName='.EFT.SM.Float.gridScan.ctZctW', wcs=['ctZ','ctW'], final=False):
        ROOT.gROOT.SetBatch(True)
        print(gridScanName)
        self.ResetHistoFile(gridScanName)

        #self.LLPlot2DEFT(gridScanName,wcs,1,False)
        self.LLPlot2DEFT(gridScanName,wcs,10,False)
        self.LLPlot2DEFT(gridScanName,wcs,30,False)
        self.LLPlot2DEFT(gridScanName,wcs,100000,False)
        # Log Plots
        #self.LLPlot2DEFT(gridScanName,wcs,1,True)
        #self.LLPlot2DEFT(gridScanName,wcs,10,True)
        #self.LLPlot2DEFT(gridScanName,wcs,100,True)

        #self.CorrelationMatrix(postScanName,False,False,freeze)
        #self.CorrelationMatrix(postScanName,True,False,freeze)

        self.ContourPlotEFT(gridScanName,wcs,final)

    def BatchBatch2DPlotsEFT(self, basenamegrid='.EFT.Float.gridScan.Jan01', allpairs=False, final=False, wcs=[]):
        ROOT.gROOT.SetBatch(True)
        
        wcs_pairs = self.wcs_pairs
        if allpairs:
            wcs_pairs = itertools.combinations(wcs,2)
            #wcs_pairs = itertools.combinations(self.wcs,2)
        else:
            wcs_pairs = [('ctW','ctG'),('ctZ','ctG'),('ctp','ctG'),('cpQM','ctG'),('cbW','ctG'),('cpQ3','ctG'),('cptb','ctG'),('cpt','ctG'),('cQl3i','ctG'),('cQlMi','ctG'),('cQei','ctG'),('ctli','ctG'),('ctei','ctG'),('ctlSi','ctG'),('ctlTi','ctG')]
            #pairs from AN
            wcs_pairs = [('cQlMi','cQei'),('cpQ3','cbW'),('cptb','cQl3i'),('ctG','cpQM'),('ctZ','ctW'),('ctei','ctlTi'),('ctlSi','ctli'),('ctp','cpt')]
            wcs_pairs = [('ctW','ctZ'),('ctG','ctZ'),('ctp','ctZ'),('cpQM','ctZ'),('cbW','ctZ'),('cpQ3','ctZ'),('cptb','ctZ'),('cpt','ctZ'),('cQl3i','ctZ'),('cQlMi','ctZ'),('cQei','ctZ'),('ctli','ctZ'),('ctei','ctZ'),('ctlSi','ctZ'),('ctlTi','ctZ')]
            wcs_pairs = [('ctp','cpt'), ('ctp','cQq11'), ('ctp','ctq1'), ('ctp','cQq81'), ('ctp','ctq8')]
            wcs_pairs = [('cQei',w) for w in self.wcs if w != 'cQei']
            wcs_pairs = [('cQq83',w) for w in self.wcs if w != 'cQq83']
            wcs_pairs = [('cQlMi',w) for w in self.wcs if w != 'cQlMi']
            # Pairs from `ptz-lj0pt_fullR2_anatest10v01_withSys.root` where abs(correlation) > 0.4
            wcs_pairs = [('cpt', 'cpQM'), ('ctlSi', 'ctlTi'), ('cQlMi', 'ctei'), ('cbW', 'cpQ3'), ('cQq81', 'cbW'), ('cbW', 'cptb'), ('cptb', 'cpQ3'), ('cQt1', 'ctt1'), ('ctp', 'ctG'), ('cQq81', 'cpQ3')]
            wcs_pairs = [('ctW','ctZ'),('ctG','ctZ'),('ctp','ctZ'),('cpQM','ctZ'),('cbW','ctZ'),('cpQ3','ctZ'),('cptb','ctZ'),('cpt','ctZ'),('cQl3i','ctZ'),('cQlMi','ctZ'),('cQei','ctZ'),('ctli','ctZ'),('ctei','ctZ'),('ctlSi','ctZ'),('ctlTi','ctZ')]
            wcs_pairs = [('ctZ', 'ctW'), ('cptb', 'cQl3i'), ('cpQ3', 'cbW')]
            wcs_pairs = [('ctp', 'cpt'), ('ctZ', 'ctW'), ('ctG', 'cpQM'), ('cptb', 'cQl3i'), ('cpQ3', 'cbW'), ('cQlMi', 'cQei')] # From TOP-19-001
            # All diagrams in TOP-22-006 or in AN
            wcs_pairs = [('cQQ1', 'cQt1'), ('cQQ1', 'cQt8'), ('cQlMi', 'cQei'), ('cQt1', 'cQt8'), ('cQt1', 'ctt1'), ('cpQ3', 'cbW'), ('cpQM', 'cpt'), ('cptb', 'cQl3i'), ('ctG', 'cpQM'), ('ctG', 'ctp'), ('ctW', 'ctZ'), ('ctp', 'cpt')]
            # All diagrams in TOP-22-006 paper
            wcs_pairs = [('ctW', 'ctZ') ,('cpQM', 'cpt') ,('ctG', 'ctp') ,('cQt1', 'cQt8') ,('cQQ1', 'cQt8') ,('cQt1', 'ctt1') ,('cQQ1', 'cQt1')]
            if len(wcs) > 0:
                wcs_pairs = []
                if isinstance(wcs, str): wcs = [wcs]
                for wc in wcs:
                    if isinstance(wc, tuple): continue
                    wcs_pairs = wcs_pairs + [(wc, other_wc) for other_wc in self.wcs if wc != other_wc]

        html = 'index.html'
        htmlFile = open(html,'w')
        htmlFile.write('<html>\n')
        htmlFile.write('<head>\n')
        htmlFile.write('    <title>Float</title>\n')
        htmlFile.write('    <style type=\'text/css\'>\n')
        htmlFile.write('        body {\n')
        htmlFile.write('            font-family: "Candara", sans-serif;\n')
        htmlFile.write('            font-size: 9pt;\n')
        htmlFile.write('            line-height: 10.5pt;\n')
        htmlFile.write('        }\n')
        htmlFile.write('        div.pic h3 {\n')
        htmlFile.write('            font-size: 11pt;\n')
        htmlFile.write('            margin: 0.5em 1em 0.2em 1em;\n')
        htmlFile.write('        }\n')
        htmlFile.write('        div.pic p {\n')
        htmlFile.write('            font-size: 11pt;\n')
        htmlFile.write('            margin: 0.2em 1em 0.1em 1em;\n')
        htmlFile.write('        }\n')
        htmlFile.write('        div.pic {\n')
        htmlFile.write('            display: block;\n')
        htmlFile.write('            float: left;\n')
        htmlFile.write('            background-color: white;\n')
        htmlFile.write('            border: 1px solid #ccc;\n')
        htmlFile.write('            padding: 2px;\n')
        htmlFile.write('            text-align: center;\n')
        htmlFile.write('            margin: 2px 10px 10px 2px;\n')
        htmlFile.write('            -moz-box-shadow: 7px 5px 5px rgb(80,80,80);    /* Firefox 3.5 */\n')
        htmlFile.write('            -webkit-box-shadow: 7px 5px 5px rgb(80,80,80); /* Chrome, Safari */\n')
        htmlFile.write('            box-shadow: 7px 5px 5px rgb(80,80,80);         /* New browsers */\n')
        htmlFile.write('        }\n')
        htmlFile.write('        a { text-decoration: none; color: rgb(80,0,0); }\n')
        htmlFile.write('        a:hover { text-decoration: underline; color: rgb(255,80,80); }\n')
        htmlFile.write('        div.dirlinks h2 {  margin-bottom: 4pt; margin-left: -24pt; color: rgb(80,0,0);  }\n')
        htmlFile.write('        div.dirlinks {  margin: 0 24pt; }\n')
        htmlFile.write('        div.dirlinks a {\n')
        htmlFile.write('            font-size: 11pt; font-weight: bold;\n')
        htmlFile.write('            padding: 0 0.5em;\n')
        htmlFile.write('        }\n')
        htmlFile.write('    </style>\n')
        htmlFile.write('</head>\n')
        htmlFile.write('<div class=\'pic\'><h3><a href="contour_leg.pdf"></h><img src ="contour_leg.png" style="width: 40px;"></a></p></div><br/>\n')
        for pair in wcs_pairs:
            # pair[0] is y-axis variable, pair[1] is x-axis variable
            #self.Batch2DPlots('{}.{}{}'.format(histosFileName,pair[0],pair[1]), '{}.{}{}'.format(basenamegrid,pair[0],pair[1]), '{}.{}{}'.format(basenamefit,pair[0],pair[1]), operators=pair, freeze=freeze)
            self.Batch2DPlotsEFT('{}.{}{}'.format(basenamegrid,pair[0],pair[1]), wcs=pair, final=final)

            if not os.path.isdir('Histos{}'.format(basenamegrid)):
                sp.call(['mkdir', 'Histos{}'.format(basenamegrid)])
                print('Created directory Histos{}'.format(basenamegrid))
            sp.call(['mv', 'Histos{}.{}{}.root'.format(basenamegrid,pair[0],pair[1]), 'Histos{}/'.format(basenamegrid)])

            for filename in os.listdir('.'):
                if filename.endswith('contour.png') or filename.endswith('contour_prelim.png') or filename.endswith('contour_final.png') or '_leg.' in filename or ('less' in filename and filename.endswith('.png')):            
                    sp.call(['mv', filename, 'Histos{}/'.format(basenamegrid)])
                if filename.endswith('contour.pdf') or filename.endswith('contour_prelim.pdf') or filename.endswith('contour_final.pdf') or '_leg.' in filename or ('less' in filename and filename.endswith('.pdf')):            
                    sp.call(['mv', filename, 'Histos{}/'.format(basenamegrid)])
                if filename.endswith('contour.eps') or filename.endswith('contour_final.eps') or ('less' in filename and filename.endswith('.eps')) or filename.endswith('contour_prelim.eps'):            
                    sp.call(['mv', filename, 'Histos{}/'.format(basenamegrid)])
                # 1D lines
                if filename.endswith('contour_1d.png') or filename.endswith('contour_final_1d.png') or ('less' in filename and filename.endswith('_1d.png')):            
                    sp.call(['mv', filename, 'Histos{}/'.format(basenamegrid)])
                if filename.endswith('contour_1d.pdf') or filename.endswith('contour_final_1d.pdf') or ('less' in filename and filename.endswith('_1d.pdf')):            
                    sp.call(['mv', filename, 'Histos{}/'.format(basenamegrid)])
                if filename.endswith('contour_1d.eps') or filename.endswith('contour_final_1d.eps') or ('less' in filename and filename.endswith('_1d.eps')) or filename.endswith('contour_prelim_1d.eps'):            
                    sp.call(['mv', filename, 'Histos{}/'.format(basenamegrid)])
            htmlFile.write('<div class=\'pic\'><h3><a href="{}less10.pdf"</h><img src ="{}less10.png" style="width: 400px;"></a></p></div>\n'.format(''.join(pair),''.join(pair)))
            htmlFile.write('<div class=\'pic\'><h3><a href="{}contour_final.pdf"></h><img src ="{}contour_final.png" style="width: 400px;"></a></p></div>\n'.format(''.join(pair),''.join(pair)))
        htmlFile.close()
        sp.call(['mv', 'index.html', 'Histos{}/'.format(basenamegrid)])

    def getIntervalFits(self,**kwargs):
        """Structured per-WC scan and interval summaries.

        siginterval=1/2 selects asymptotic reference thresholds 1/4.
        nll_max remains an explicit numerical override for siginterval=2.
        """
        basenames = kwargs.pop('basename_lst', ['.EFT.SM.Float'])
        basenames = [basenames] if isinstance(basenames, str) else basenames
        params = self._selected_wcs(kwargs.pop('params', []))
        siginterval = kwargs.pop('siginterval', 2)
        if siginterval not in (1, 2):
            raise ValueError("siginterval must be 1 or 2; use threshold for an explicit numerical level")
        nll_max = kwargs.pop('nll_max', 4)
        threshold = kwargs.pop('threshold', 1 if siginterval == 1 else nll_max)
        results = []
        for wc in params:
            scan = self.read_scan_result(self.AppendStrToItemsInLst(basenames, '.' + wc), wc, **kwargs)
            intervals = scan.intervals(threshold)
            results.append({'scan': scan, 'intervals': intervals, 'failed_points': scan.failed_points})
        return results

    def BestScanPlot(self, basename_float_lst=[''], basename_freeze_lst=[''], final=False, titles = ['Others profiled', 'Others fixed to SM'], filename='', wcs=[], printFOM=False, asimov_plotstyle_flag=False, **kwargs):
        names1 = [basename_float_lst] if isinstance(basename_float_lst, str) else basename_float_lst
        names2 = [basename_freeze_lst] if isinstance(basename_freeze_lst, str) else basename_freeze_lst
        options1 = dict(kwargs.pop('profiled_options', {}))
        options2 = dict(kwargs.pop('frozen_options', {}))
        options1 = dict(kwargs, **options1)
        options2 = dict(kwargs, **options2)
        options1.setdefault('family', 'profiled')
        options2.setdefault('family', 'frozen')
        rows = [(wc,
                 self.read_scan_result(self.AppendStrToItemsInLst(names1, '.' + wc), wc, **options1),
                 self.read_scan_result(self.AppendStrToItemsInLst(names2, '.' + wc), wc, **options2))
                for wc in self._selected_wcs(wcs)]
        ratios = {}
        if printFOM:
            print('FoM = frozen interval width / profiled interval width; undefined for open or disconnected intervals')
            for wc, profiled, frozen in rows:
                ratios[wc] = {threshold: interval_width_ratio(profiled.intervals(threshold), frozen.intervals(threshold)) for threshold in (1, 4)}
                print(wc, ratios[wc])
        rendered = render_summary(rows, titles, self.display,
                                  'BestScanPlot' + filename + ('_final' if final else '_prelim'),
                                  asimov=asimov_plotstyle_flag)
        return dict(rendered, rows=tuple(rows), width_ratios=ratios)

    def BestFitPlot(self):
        """The embedded historical fit-number demonstration has been retired."""
        raise ValueError("BestFitPlot embedded historical fit numbers are retired; use BestScanPlot with actual result inputs")


if __name__ == "__main__":
    log_file = 'EFTFit_out.log'

    FORMAT1 = '%(message)s'
    FORMAT2 = '[%(levelname)s] %(message)s'
    FORMAT3 = '[%(levelname)s][%(name)s] %(message)s'

    frmt1 = logging.Formatter(FORMAT1)
    frmt2 = logging.Formatter(FORMAT2)
    frmt3 = logging.Formatter(FORMAT3)

    logging.basicConfig(
        level=logging.DEBUG,
        format=FORMAT2,
        filename=log_file,
        filemode='w'
    )

    # Configure logging to also output to stdout
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(frmt2)
    logging.getLogger('').addHandler(console)

    plotter = EFTPlot()
    #plotter.ContourQuick()
    #plotter.Batch2DPlots('.EFT.SM.Float.ctWctZ','.EFT.SM.Float.ctWctZ','.EFT.SM.Float.postScan')
    #plotter.LLPlot1D('.EFT.SM.Float.ctW','ctW')
    #plotter.OverlayLLPlot1D('.EFT.SM.Float.ctW','.EFT.SM.Freeze.ctW','ctW')
    #plotter.BatchOverlayLLPlot1D()
        
