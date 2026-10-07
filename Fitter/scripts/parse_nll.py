import uproot
import numpy as np
import matplotlib.pyplot as plt

# Historical best-point helpers. WC membership must be supplied by the caller.
from EFTFit.Fitter.scan_1d import canonicalize_entries, ScanEntry, finite
from EFTFit.Fitter.execution_provenance import resolve_canonical_aggregate

########################################################################
### Helper functions ###

# Get path to root file given base name
#   - Assumes the file is in "../fit_files" relative to where we are
#   - Requires the existing identity-bound canonical aggregate contract
def find_root_file_path(base_name,wc):
    return resolve_canonical_aggregate(base_name + "." + wc)


# Get the values of params from the root file
def get_vals_from_root_file(root_file_path,branches_to_get,srip_poi_branch_names=False):

    with uproot.open(root_file_path) as source:
        if "limit" not in source:
            raise ValueError("missing limit tree")
        tree = source["limit"]
        if not branches_to_get or set(branches_to_get) - set(tree.keys()):
            raise ValueError("missing required branches or empty branch selection")
        columns = tree.arrays(branches_to_get, library="np", how=dict)
        if not len(columns[branches_to_get[0]]):
            raise ValueError("empty result")
        result = {}
        for name, values in columns.items():
            key = name.removeprefix("trackedParam_") if srip_poi_branch_names else name
            if key in result:
                raise ValueError("branch name collision after stripping trackedParam_")
            result[key] = [finite(value, name) for value in values]
        return result


# Gets list of branches names
#   - Assumes the non-scanned POIs have the name trackedParam_NAME
#   - Assumes you also want delta NLL
def make_branches_lst(poi_lst,scan_poi):
    out_lst = ["deltaNLL"]
    out_lst.append(scan_poi)
    for poi in poi_lst:
        if poi == scan_poi: continue
        out_lst.append("trackedParam_"+poi)
    return out_lst


# Get arrays that have only one copy of each scan point
#   - Assuming the scan was along scan_var
#   - In case of multiple scan_var values, choose the one with the min minimize_var value
def get_unique_points(in_dict,scan_var,minimize_var):

    size = len(in_dict[scan_var])
    if any(len(values) != size for values in in_dict.values()):
        raise ValueError("unequal column lengths")
    scan = canonicalize_entries(scan_var, [ScanEntry(x, y, "grid", locator=str(index))
        for index, (x, y) in enumerate(zip(in_dict[scan_var], in_dict[minimize_var]))])
    # Companion columns follow the winning entry; every coordinate/NLL is validated.
    indices = [int(point.entries[0].locator) for point in scan.points]
    return {key: [values[index] for index in indices] for key, values in in_dict.items()}

########################################################################
### Plotting functions ###

def make_scatter_plot(lst_of_points_to_plot,xaxis_ticklabels_lst,save_name="test"):

    fig, axs = plt.subplots(nrows=1, ncols=1, figsize=(20,7))

    y_name = "WC value"
    axs.set_ylabel(y_name)
    axs.set_xlabel("WC")
    axs.set_title("Best fit points summary")
    axs.grid()
    axs.set_xticks(np.arange(0,len(xaxis_ticklabels_lst),step=1))
    axs.set_xticklabels(xaxis_ticklabels_lst)

    # Main plot
    for i,plot_info_dict in enumerate(lst_of_points_to_plot):
        xarr = plot_info_dict["xarr"]
        yarr = plot_info_dict["yarr"]
        leg_str = plot_info_dict["label"]
        clr = plot_info_dict["color"]
        marker = plot_info_dict["marker"]
        plt.scatter(xarr,yarr,color=clr,marker=marker,edgecolors='none',label=leg_str, zorder=10)

    # Legend
    axs.legend(prop={'size': 10},scatterpoints=1)

    plt.axhline(y = 0.0, color = 'k', linestyle = '-',zorder=8) # hline at 1
    #plt.xlim(0,80)
    #plt.ylim(0,100)

    plt.savefig(save_name+".png",format="png")
    plt.show()

    return plt

def plotter_wrapper(lst_of_bestfit_dicts, poi_lst=None):
    if not poi_lst:
        raise ValueError("poi_lst from the fit configuration is required")
    labels = list(poi_lst) + ["deltaNLL"]
    points = [{"label": name + " scan", "color": "C{}".format(index % 10),
               "marker": "o", "yarr": [values[key] for key in labels],
               "xarr": list(range(len(labels)))}
              for index, (name, values) in enumerate(lst_of_bestfit_dicts.items())]
    return make_scatter_plot(points, labels)


########################################################################
### Wrapper functions ###

# Find the best points in EFT space
def get_best_nll_eft_point(in_dict,poi_lst):
    best_point_dict = {}
    best_nll_idx = in_dict["deltaNLL"].index(min(in_dict["deltaNLL"]))
    for poi_name in poi_lst + ["deltaNLL"]:
        poi_val = in_dict[poi_name]
        best_point_dict[poi_name] = in_dict[poi_name][best_nll_idx]

    return best_point_dict

# Take dict like this ctG_scan_dict = {"ctG":1.1,"ctW":2.1}, ctW_scan_dict = {"ctG":1.2,"ctW"2.2} -> ctG_vals=[1.1,1.2] ctW_vals=[2.1,2.2]
def get_best_points_by_wc(lst_of_best_point_dicts, poi_lst=None):
    if not poi_lst:
        raise ValueError("poi_lst from the fit configuration is required")
    best_point_summary = {}
    for poi in poi_lst:
        best_point_summary[poi] = []
    for best_point_dict in lst_of_best_point_dicts:
        for poi in poi_lst:
            best_point_summary[poi].append(best_point_dict[poi])

    return best_point_summary


########################################################################
def main():
    import argparse
    from EFTFit.Fitter.wc_fit_config import load_fit_config
    parser = argparse.ArgumentParser(description="Inspect canonical scan best points using explicit WC metadata")
    parser.add_argument("--fit-config", required=True)
    parser.add_argument("--basename", required=True)
    args = parser.parse_args()
    poi_lst = list(load_fit_config(args.fit_config).scientific_wcs)
    bestfit_dict = {}
    for poi_name in poi_lst:
        root_dict = get_vals_from_root_file(find_root_file_path(args.basename, poi_name),
                                           make_branches_lst(poi_lst, poi_name), True)
        unique = get_unique_points(root_dict, poi_name, "deltaNLL")
        bestfit_dict[poi_name] = get_best_nll_eft_point(unique, poi_lst)
    plotter_wrapper(bestfit_dict, poi_lst)


if __name__ == "__main__":
    main()
