""" Batch run the flux code using a flux batch file. """

import datetime
import os
import shlex
import sys
import collections
import copy
    

import ephem
import numpy as np
import scipy
import matplotlib.pyplot as plt
from matplotlib import scale as mscale
from cycler import cycler


from RMS.Astrometry.Conversions import datetime2JD, jd2Date
from RMS.Formats.FTPdetectinfo import findFTPdetectinfoFile
from RMS.Formats.Showers import FluxShowers, loadRadiantShowers
from Utils.Flux import calculatePopulationIndex, calculateMassIndex, computeFlux, detectClouds, fluxParser, \
    calculateFixedBins, calculateZHR, massVerniani, loadShower
from RMS.Routines.SolarLongitude import unwrapSol
from RMS.Misc import formatScientific, SegmentedScale

# Now that the Scale class has been defined, it must be registered so
# that ``matplotlib`` can find it.
mscale.register_scale(SegmentedScale)


def addFixedBins(sol_bins, small_sol_bins, small_dt_bins, meteor_num_arr, collecting_area_arr, obs_time_arr, \
    lm_m_arr, rad_elev_arr, rad_dist_arr, ang_vel_arr):
    """ Sort data into fixed bins by solar longitude. 

    For a larger array of solar longitudes sol_bins, fits parameters to an empty array of its size (minus 1)
    so that small_sol_bins agrees with sol_bins

    Assumes that for some index i, sol_bins[i:i+len(small_sol_bins)] = small_sol_bins. If this is not true,
    then the values are invalid and different small arrays should be used

    Arguments:
        sol_bins: [ndarray] Array of solar longitude bin edges. Does not wrap around
        small_sol_bins: [ndarray] Array of solar longitude bin edges which is smaller in length than
            sol_bins but can be transformed to sol_bins if shifted by a certain index. Does not wrap
            around.
        small_dt_bins: [ndarray] Datetime objects corresponding to the small_sol_bins edges. NOT USED.
        *params: [ndarray] Physical quantities such as number of meteors, collecting area.

    Return:
        [tuple] Same variables corresponding to params
            - val: [ndarray] Array of where any index that used to correspond to a sol in small_sol_bins,
                now corresponds to an index in sol_bins, padding all other values with zeros
    """

    # if sol_bins wraps would wrap around but forced_bins_sol doesn't
    if sol_bins[0] > small_sol_bins[0]:
        i = np.argmax(sol_bins - (small_sol_bins[0] + 360) > -1e-7)
    else:
        i = np.argmax(sol_bins - small_sol_bins[0] > -1e-7)  # index where they are equal


    # # Sort datetime edges into bins
    # dt_binned = np.zeros(len(sol_bins), dtype="datetime64[ms]")
    # dt_binned[i:i + len(small_dt_bins)] = small_dt_bins

    # Sort collecting area into bins
    collecting_area_binned = np.zeros(len(sol_bins) - 1)
    collecting_area_binned[i:i + len(collecting_area_arr)] = collecting_area_arr

    # Sort observation time into bins
    obs_time_binned = np.zeros(len(sol_bins) - 1)
    obs_time_binned[i:i + len(obs_time_arr)] = obs_time_arr

    # Sort meteor limiting magnitude into bins
    lm_m_binned = np.zeros(len(sol_bins) - 1) + np.nan
    lm_m_binned[i:i + len(obs_time_arr)] = lm_m_arr

    # Sort radiant elevation into bins
    rad_elev_binned = np.zeros(len(sol_bins) - 1) + np.nan
    rad_elev_binned[i:i + len(obs_time_arr)] = rad_elev_arr

    # Sort radiant distance into bins
    rad_dist_binned = np.zeros(len(sol_bins) - 1) + np.nan
    rad_dist_binned[i:i + len(obs_time_arr)] = rad_dist_arr

    # Sort angular velocity into bins
    ang_vel_binned = np.zeros(len(sol_bins) - 1) + np.nan
    ang_vel_binned[i:i + len(obs_time_arr)] = ang_vel_arr


    # Sort meteor numbers into bins
    meteor_num_binned = np.zeros(len(sol_bins) - 1)
    meteor_num_binned[i:i + len(meteor_num_arr)] = meteor_num_arr

    # Set the number of meteors to zero where either the time or the collecting area is also zero
    meteor_num_binned[(collecting_area_binned == 0) | (obs_time_binned == 0)] = 0

    #data_arrays = []
    # for p in params:
    #     forced_bin_param = np.zeros(len(sol_bins) - 1)
    #     forced_bin_param[i:i + len(p)] = p
    #     data_arrays.append(forced_bin_param)

    return [meteor_num_binned, collecting_area_binned, obs_time_binned, lm_m_binned, rad_elev_binned, \
        rad_dist_binned, ang_vel_binned]


def combineFixedBinsAndComputeFlux(
    sol_bins, meteors, time_area_prod, lm_m_data, rad_elev_data, rad_dist_data, ang_vel_data, min_meteors=50,\
    ci=0.95, min_tap=2, min_bin_duration=0.5, max_bin_duration=12):
    """
    Computes flux values and their corresponding solar longitude based on bins containing
    number of meteors, and time-area product. Bins will be combined so that each bin has the
    minimum number of meteors

    Arguments:
        sol_bins: [ndarray] Solar longitude of bins start and end (the length must be 1 more than meteors)
        meteors: [ndarray] Number of meteors in a bin
        time_area_prod: [ndarray] Time multiplied by LM corrected collecting area added for each station
            which contains each bin
        lm_m_data: [ndarray]
        rad_elev_data: [ndarray]
        rad_dist_data: [ndarray]
        ang_vel_data: [ndarray]

    Keyword arguments:
        min_meteors: [int] Minimum number of meteors to have in a bin
        ci: [float] Confidence interval for calculating the flux error bars (from 0 to 1)
        min_tap: [float] Minimum time area product in 1000 km^2*h.
        min_bin_duration: [float] Minimum bin duration in hours.
        max_bin_duration: [float] Maximum bin duration in hours.

    Return:
        [tuple] sol, flux, flux_lower, flux_upper, meteors, ta_prod
            - sol: [ndarray] Solar longitude
            - flux: [ndarray] Flux corresponding to solar longitude
            - flux_lower: [ndarray] Lower bound of flux corresponding to sol
            - flux_upper: [ndarray] Upper bound of flux corresponding to sol
            - meteor_count: [ndarray] Number of meteors in bin
            - time_area_product: [ndarray] Time area product of bin

    """
    middle_bin_sol = (sol_bins[1:] + sol_bins[:-1])/2

    flux_list = []
    flux_upper_list = []
    flux_lower_list = []
    sol_list = []
    sol_bin_list = []
    meteor_count_list = []
    time_area_product_list = []
    lm_m_list = []
    rad_elev_list = []
    rad_dist_list = []
    ang_vel_list = []

    start_idx = 0
    for end_idx in range(1, len(meteors)):

        sl = slice(start_idx, end_idx)

        # Compute the total duration of the bin (convert from solar longitude)
        bin_hours = (middle_bin_sol[end_idx] - middle_bin_sol[start_idx])/(2*np.pi)*24*365.24219

        # If the number of meteors, time-area product, and duration are larger than the limits, add this as 
        #   a new bin
        if (np.sum(meteors[sl]) >= min_meteors) and (np.nansum(time_area_prod[sl])/1e9 >= min_tap) \
            and (bin_hours >= min_bin_duration):

            # Sum up the values in the bin
            ta_prod = np.sum(time_area_prod[sl])
            num_meteors = np.sum(meteors[sl])

            meteor_count_list.append(num_meteors)
            time_area_product_list.append(ta_prod)

            if ta_prod == 0:
                flux_list.append(np.nan)
                flux_upper_list.append(np.nan)
                flux_lower_list.append(np.nan)
                lm_m_list.append(np.nan)
                rad_elev_list.append(np.nan)
                rad_dist_list.append(np.nan)
                ang_vel_list.append(np.nan)

            else:

                # Compute Poisson errors
                n_meteors_upper = scipy.stats.chi2.ppf(0.5 + ci/2, 2*(num_meteors + 1))/2
                n_meteors_lower = scipy.stats.chi2.ppf(0.5 - ci/2, 2*num_meteors)/2

                # Compute the flux
                flux_list.append(1e9*num_meteors/ta_prod)
                flux_upper_list.append(1e9*n_meteors_upper/ta_prod)
                flux_lower_list.append(1e9*n_meteors_lower/ta_prod)

                # Compute the TAP-weighted meteor limiting magnitude
                lm_m_select = lm_m_data[sl]*time_area_prod[sl]
                lm_m_weighted = np.sum(lm_m_select[~np.isnan(lm_m_select)])/ta_prod
                lm_m_list.append(lm_m_weighted)

                # Compute the TAP-weighted radiant elevation
                rad_elev_select = rad_elev_data[sl]*time_area_prod[sl]
                rad_elev_weighted = np.sum(rad_elev_select[~np.isnan(rad_elev_select)])/ta_prod
                rad_elev_list.append(rad_elev_weighted)

                # Compute the TAP-weighted radiant distance
                rad_dist_select = rad_dist_data[sl]*time_area_prod[sl]
                rad_dist_weighted = np.sum(rad_dist_select[~np.isnan(rad_dist_select)])/ta_prod
                rad_dist_list.append(rad_dist_weighted)

                # Compute the TAP-weighted angular velocity
                ang_vel_select = ang_vel_data[sl]*time_area_prod[sl]
                ang_vel_weighted = np.sum(ang_vel_select[~np.isnan(ang_vel_select)])/ta_prod
                ang_vel_list.append(ang_vel_weighted)


            sol_list.append(np.mean(middle_bin_sol[sl]))
            sol_bin_list.append(sol_bins[start_idx])
            start_idx = end_idx

        # If the total duration is over the maximum duration, skip the bin
        elif bin_hours >= max_bin_duration:
            start_idx = end_idx

    sol_bin_list.append(sol_bins[start_idx])

    return (
        np.array(sol_list),
        np.array(sol_bin_list),
        np.array(flux_list),
        np.array(flux_lower_list),
        np.array(flux_upper_list),
        np.array(meteor_count_list),
        np.array(time_area_product_list),
        np.array(lm_m_list),
        np.array(rad_elev_list),
        np.array(rad_dist_list),
        np.array(ang_vel_list),
    )


class StationPlotParams:
    '''Class to give plots specific appearances based on the station'''

    def __init__(self):
        self.color_dict = {}
        self.marker_dict = {}
        self.markers = ['o', 'x', '+']

        self.color_cycle = [plt.get_cmap("tab10")(i) for i in range(10)]

    def __call__(self, station):
        if station not in self.color_dict:
            # Generate a new color
            color = self.color_cycle[len(self.color_dict)%(len(self.color_cycle))]
            label = station
            marker = self.markers[(len(self.marker_dict) // 10)%(len(self.markers))]

            # Assign plot color
            self.color_dict[station] = color
            self.marker_dict[station] = marker

        else:
            color = self.color_dict[station]
            marker = self.marker_dict[station]
            # label = str(config.stationID)
            label = None

        #return {'color': color, 'marker': marker, 'label': label}

        # Don't include the station name in the legend
        return {'color': color, 'marker': marker}



def cameraTally(comb_sol, comb_sol_bins, single_fixed_bin_information):
    """ Tally contributions from individual cameras in every time bin. 
        
    Arguments:
        comb_sol: [list] List of combined mean solar longitues (degrees).
        comb_sol_bins: [list] List of combined solar longitue bin edges (degrees).
        single_fixed_bin_information: [list] A list of [station, [sol (rad)], [meteor_num], [area (m^2)], 
            [time_bin (hour)]] entries for every station.
    """

    bin_tally = collections.OrderedDict()
    bin_tally_topmeteors = collections.OrderedDict()
    bin_tally_toptap = collections.OrderedDict()

    # Go through all solar longitude bins
    for i in range(len(comb_sol_bins) - 1):

        sol_start = np.radians(comb_sol_bins[i])
        sol_end   = np.radians(comb_sol_bins[i + 1])
        sol_mean  = comb_sol[i]

        # Add an entry for the bin
        if sol_mean not in bin_tally:
            bin_tally[sol_mean] = collections.OrderedDict()


        # Compute station contributions
        for station, (sol_arr, _, met_num, area, time_bin, lm_m, _, _, _) in single_fixed_bin_information:

            sol_arr = np.array(sol_arr)
            met_num = np.array(met_num)
            area = np.array(area)
            time_bin = np.array(time_bin)

            # Select data in the solar longitude range
            sol_arr_unwrapped = unwrapSol(sol_arr[:-1], sol_start, sol_end)
            mask_arr = (sol_arr_unwrapped >= sol_start) & (sol_arr_unwrapped <= sol_end)

            # Set the number of meteors to 0 where the TAP or the observing duration are 0
            met_num[(area == 0) | (time_bin == 0)] = 0

            if np.any(mask_arr):

                if station not in bin_tally[sol_mean]:
                    
                    # Add an entry for the station, if it doesn't exist
                    bin_tally[sol_mean][station] = {'meteors': 0, 'tap': 0}

                # Add numbers to the tally
                bin_tally[sol_mean][station]['meteors'] += np.sum(met_num[mask_arr])
                bin_tally[sol_mean][station]['tap'] += np.sum(area[mask_arr]*time_bin[mask_arr])


        # Sort by the number of meteors
        bin_cams_meteors = bin_tally[sol_mean]
        bin_cams_meteors = collections.OrderedDict(sorted(bin_cams_meteors.items(), \
            key=lambda item: item[1]['meteors'], reverse=True))
        bin_tally_topmeteors[sol_mean] = bin_cams_meteors

        # Sort by the TAP
        bin_cams_tap = bin_tally[sol_mean]
        bin_cams_tap = collections.OrderedDict(sorted(bin_cams_tap.items(), key=lambda item: item[1]['tap'], \
            reverse=True))
        bin_tally_toptap[sol_mean] = bin_cams_tap



    return bin_tally_topmeteors, bin_tally_toptap






if __name__ == "__main__":

    import argparse

    import RMS.ConfigReader as cr

    # Init the command line arguments parser
    arg_parser = argparse.ArgumentParser(
        description="Compute multi-station and multi-year meteor shower flux from a batch file."
    )

    arg_parser.add_argument("batch_path", metavar="BATCH_PATH", type=str, help="Path to the flux batch file.")

    arg_parser.add_argument(
        "--output_filename",
        metavar="FILENAME",
        type=str,
        default='fluxbatch_output',
        help="Filename to export images and data (exclude file extensions), defaults to fluxbatch_output",
    )

    arg_parser.add_argument(
        "-csv",
        action='store_true',
        help="If given, will read from the csv files defined with output_filename (defaults to fluxbatch_output)",
    )

    arg_parser.add_argument(
        "--single",
        action='store_true',
        help="Show single-station fluxes.",
    )

    arg_parser.add_argument(
        "--onlyflux",
        action='store_true',
        help="Only plot the flux, without the additional plots.",
    )

    arg_parser.add_argument(
        "--minmeteors",
        type=int,
        default=30,
        help="Minimum meteors per bin. If this is not satisfied the bin will be made larger. Default = 30 meteors.",
    )

    arg_parser.add_argument(
        "--mintap",
        type=float,
        default=3,
        help="Minimum time-area product per bin. If this is not satisfied the bin will be made larger. Default = 3 x 1000 km^2 h.",
    )

    arg_parser.add_argument(
        "--minduration",
        type=float,
        default=0.5,
        help="Minimum time per bin in hours. If this is not satisfied the bin will be made larger. Default = 0.5 h.",
    )

    arg_parser.add_argument(
        "--maxduration",
        type=float,
        default=12,
        help="Maximum time per bin in hours. If this is not satisfied, the bin will be discarded. Default = 12 h.",
    )

    # Parse the command line arguments
    fluxbatch_cml_args = arg_parser.parse_args()

    #########################


    # Only run in Python 3+
    if sys.version_info[0] < 3:
        print("The flux code can only run in Python 3+ !")
        sys.exit()


    ### Binning parameters ###

    # Confidence interval
    ci = 0.95

    # Base bin duration (minutes)
    bin_duration = 5

    # Minimum number of meteors in the bin
    min_meteors = fluxbatch_cml_args.minmeteors

    # Minimum time-area product (1000 km^2 h)
    min_tap = fluxbatch_cml_args.mintap

    # Minimum bin duration (hours)
    min_bin_duration = fluxbatch_cml_args.minduration

    # Maximum bin duration (hours)
    max_bin_duration = fluxbatch_cml_args.maxduration

    ### ###


    # Check if the batch file exists
    if not os.path.isfile(fluxbatch_cml_args.batch_path):
        print("The given batch file does not exist!", fluxbatch_cml_args.batch_path)
        sys.exit()

    dir_path = os.path.dirname(fluxbatch_cml_args.batch_path)

    output_data = []
    shower_code = None
    summary_population_index = []



    plot_info = StationPlotParams()


    # Init the plot
    if fluxbatch_cml_args.onlyflux:
        subplot_rows = 1
    else:
        subplot_rows = 4
    fig, ax = plt.subplots(nrows=subplot_rows, figsize=(15, 10), sharex=True, \
        gridspec_kw={'height_ratios': [3, 1, 1, 1][:subplot_rows]})


    if not isinstance(ax, np.ndarray):
        ax = [ax]


    # If an input CSV file was not given, compute the data
    if not fluxbatch_cml_args.csv:

        # loading commands from batch file and collecting information to run computeflux, including
        # detecting the clouds

        file_data = []
        with open(fluxbatch_cml_args.batch_path) as f:

            # Parse the batch entries
            for line in f:
                line = line.replace("\n", "").replace("\r", "")

                if not len(line):
                    continue

                if line.startswith("#"):
                    continue

                flux_cml_args = fluxParser().parse_args(shlex.split(line, posix=0))
                (
                    ftpdetectinfo_path,
                    shower_code,
                    mass_index,
                    binduration,
                    binmeteors,
                    time_intervals,
                    fwhm,
                    ratio_threshold,
                    ref_ht
                ) = (
                    flux_cml_args.ftpdetectinfo_path,
                    flux_cml_args.shower_code,
                    flux_cml_args.massindex,
                    flux_cml_args.binduration,
                    flux_cml_args.binmeteors,
                    flux_cml_args.timeinterval,
                    flux_cml_args.fwhm,
                    flux_cml_args.ratiothres,
                    flux_cml_args.ht,
                )
                ftpdetectinfo_path = findFTPdetectinfoFile(ftpdetectinfo_path)

                if not os.path.isfile(ftpdetectinfo_path):
                    print("The FTPdetectinfo file does not exist:", ftpdetectinfo_path)
                    print("Exiting...")
                    sys.exit()

                # Extract parent directory
                ftp_dir_path = os.path.dirname(ftpdetectinfo_path)

                # Load the config file
                try:
                    config = cr.loadConfigFromDirectory('.', ftp_dir_path)

                except RuntimeError:
                    print("The config file could not be loaded! Skipping...")
                    continue

                if time_intervals is None:
                    
                    # Find time intervals to compute flux with
                    print('Detecting whether clouds are present...')

                    time_intervals = detectClouds(
                        config, ftp_dir_path, show_plots=False, ratio_threshold=ratio_threshold
                    )

                    print('Cloud detection complete!')
                    print()

                else:
                    dt_beg_temp = datetime.datetime.strptime(time_intervals[0], "%Y%m%d_%H%M%S")
                    dt_end_temp = datetime.datetime.strptime(time_intervals[1], "%Y%m%d_%H%M%S")
                    time_intervals = [[dt_beg_temp, dt_end_temp]]


                file_data.append(
                    [
                        config,
                        ftp_dir_path,
                        ftpdetectinfo_path,
                        shower_code,
                        time_intervals,
                        mass_index,
                        binduration,
                        binmeteors,
                        fwhm,
                        ref_ht
                    ]
                )


        # Load the shower object from the given shower code
        shower = loadShower(config, shower_code, mass_index)

        # Init the apparent speed
        _, _, v_init = shower.computeApparentRadiant(0, 0, 2451545.0)

        # Compute the mass limit at 6.5 mag
        mass_lim = massVerniani(6.5, v_init/1000)

        # Override the mass index if given
        if mass_index is not None:
            shower.mass_index = mass_index



        print()
        print("Calculating fixed bins...")

        # Compute 5 minute bins of equivalent solar longitude every year
        sol_bins, bin_datetime_yearly = calculateFixedBins(
            [time_interval for data in file_data for time_interval in data[4]],
            [data[1] for data in file_data],
            shower,
            bin_duration=bin_duration)


        all_fixed_bin_information = []
        single_fixed_bin_information = []

        # Compute the flux
        for (config, ftp_dir_path, ftpdetectinfo_path, shower_code, time_intervals, s, binduration, \
            binmeteors, fwhm, ref_ht) in file_data:

            # Compute the flux in every observing interval
            for interval in time_intervals:

                dt_beg, dt_end = interval

                # Extract datetimes of forced bins relevant for this time interval
                dt_bins = bin_datetime_yearly[np.argmax([year_start < dt_beg < year_end \
                    for (year_start, year_end), _ in bin_datetime_yearly])][1]

                forced_bins = (dt_bins, sol_bins)

                ret = computeFlux(
                    config,
                    ftp_dir_path,
                    ftpdetectinfo_path,
                    shower_code,
                    dt_beg,
                    dt_end,
                    s,
                    binduration=binduration,
                    binmeteors=binmeteors,
                    ref_height=ref_ht,
                    show_plots=False,
                    default_fwhm=fwhm,
                    confidence_interval=ci,
                    forced_bins=forced_bins,
                    compute_single=fluxbatch_cml_args.single,
                )

                if ret is None:
                    continue
                (
                    sol_data,
                    flux_lm_6_5_data,
                    flux_lm_6_5_ci_lower_data,
                    flux_lm_6_5_ci_upper_data,
                    meteor_num_data,
                    population_index,
                    bin_information,
                ) = ret

                # Skip observations with no computed fixed bins
                if len(bin_information[0]) == 0:
                    continue

                # Sort measurements into fixed bins
                all_fixed_bin_information.append(addFixedBins(sol_bins, *bin_information))

                single_fixed_bin_information.append([config.stationID, bin_information])
                summary_population_index.append(population_index)


                # Store and plot single-station data
                if fluxbatch_cml_args.single:

                    # Add computed flux to the output list
                    output_data += [
                        [config.stationID, sol, flux, lower, upper, population_index]
                        for (sol, flux, lower, upper) in zip(
                            sol_data, flux_lm_6_5_data, flux_lm_6_5_ci_lower_data, flux_lm_6_5_ci_upper_data
                        )
                    ]

                    

                    # plot data for night and interval
                    plot_params = plot_info(config.stationID)

                    # Plot the single-station flux line
                    line = ax[0].plot(sol_data, flux_lm_6_5_data, linestyle='dashed', **plot_params)

                    # Plot single-station error bars
                    ax[0].errorbar(
                        sol_data,
                        flux_lm_6_5_data,
                        color=plot_params['color'],
                        alpha=0.5,
                        capsize=5,
                        zorder=3,
                        linestyle='none',
                        yerr=[
                            np.array(flux_lm_6_5_data) - np.array(flux_lm_6_5_ci_lower_data),
                            np.array(flux_lm_6_5_ci_upper_data) - np.array(flux_lm_6_5_data),
                        ],
                    )


        # Sum meteors in every bin (this is a 2D along the first axis, producing an array)
        num_meteors = sum(np.array(meteors) for meteors, _, _, _, _, _, _ in all_fixed_bin_information)

        # Compute time-area product in every bin
        time_area_product = sum(np.array(area)*np.array(time) for _, area, time, _, _, _, \
            _ in all_fixed_bin_information)

        # Compute TAP-wieghted meteor limiting magnitude in every bin
        lm_m_data = np.zeros_like(num_meteors)
        for _, area, time, lm_m, _, _, _ in all_fixed_bin_information:

            lm_m_data[~np.isnan(lm_m)] += (
                 np.array(lm_m[~np.isnan(lm_m)])
                *np.array(area[~np.isnan(lm_m)])
                *np.array(time[~np.isnan(lm_m)])
                )

        lm_m_data /= time_area_product

        # Compute TAP-wieghted radiant elevation in every bin
        rad_elev_data = np.zeros_like(num_meteors)
        for _, area, time, _, rad_elev, _, _ in all_fixed_bin_information:

            rad_elev_data[~np.isnan(rad_elev)] += (
                 np.array(rad_elev[~np.isnan(rad_elev)])
                *np.array(area[~np.isnan(rad_elev)])
                *np.array(time[~np.isnan(rad_elev)])
                )

        rad_elev_data /= time_area_product


        # Compute TAP-wieghted radiant distance in every bin
        rad_dist_data = np.zeros_like(num_meteors)
        for _, area, time, _, _, rad_dist, _ in all_fixed_bin_information:

            rad_dist_data[~np.isnan(rad_dist)] += (
                 np.array(rad_dist[~np.isnan(rad_dist)])
                *np.array(area[~np.isnan(rad_dist)])
                *np.array(time[~np.isnan(rad_dist)])
                )

        rad_dist_data /= time_area_product


        # Compute TAP-wieghted angular velocity in every bin
        ang_vel_data = np.zeros_like(num_meteors)
        for _, area, time, _, _, _, ang_vel in all_fixed_bin_information:

            ang_vel_data[~np.isnan(ang_vel)] += (
                 np.array(ang_vel[~np.isnan(ang_vel)])
                *np.array(area[~np.isnan(ang_vel)])
                *np.array(time[~np.isnan(ang_vel)])
                )

        ang_vel_data /= time_area_product


        (
            comb_sol,
            comb_sol_bins,
            comb_flux,
            comb_flux_lower,
            comb_flux_upper,
            comb_num_meteors,
            comb_ta_prod,
            comb_lm_m,
            comb_rad_elev,
            comb_rad_dist,
            comb_ang_vel,
        ) = combineFixedBinsAndComputeFlux(
            sol_bins,
            num_meteors,
            time_area_product,
            lm_m_data,
            rad_elev_data,
            rad_dist_data,
            ang_vel_data,
            ci=ci,
            min_tap=min_tap,
            min_meteors=min_meteors,
            min_bin_duration=min_bin_duration,
            max_bin_duration=max_bin_duration,
        )
        comb_sol = np.degrees(comb_sol)
        comb_sol_bins = np.degrees(comb_sol_bins)


        # Compute the weighted mean meteor magnitude
        lm_m_mean = np.sum(comb_lm_m*comb_ta_prod)/np.sum(comb_ta_prod)

        # Compute the mass limit at the mean meteor LM
        mass_lim_lm_m_mean = massVerniani(lm_m_mean, v_init/1000)

        print("Mean TAP-weighted meteor limiting magnitude = {:.2f}M".format(lm_m_mean))
        print("                         limiting mass      = {:.2e} g".format(1000*mass_lim_lm_m_mean))

        # Compute the mean population index
        population_index_mean = np.mean(summary_population_index)

        # Compute the flux conversion factor
        lm_m_to_6_5_factor = population_index_mean**(6.5 - lm_m_mean)

        # Compute the flux to the mean meteor limiting magnitude
        comb_flux_lm_m = comb_flux/lm_m_to_6_5_factor
        comb_flux_lm_m_lower = comb_flux_lower/lm_m_to_6_5_factor
        comb_flux_lm_m_upper = comb_flux_upper/lm_m_to_6_5_factor


        # Compute the ZHR
        comb_zhr = calculateZHR(comb_flux, population_index_mean)
        comb_zhr_lower = calculateZHR(comb_flux_lower, population_index_mean)
        comb_zhr_upper = calculateZHR(comb_flux_upper, population_index_mean)


        ### Print camera tally ###

        # Tally up contributions from individual cameras in each bin
        bin_tally_topmeteors, bin_tally_toptap = cameraTally(comb_sol, comb_sol_bins, \
            single_fixed_bin_information)

        print()
        print("Camera tally per bin:")
        print("---------------------")

        # Print cameras with most meteors per bin
        for sol_bin_mean in bin_tally_topmeteors:

            # Get cameras with most meteors
            bin_cams_topmeteors = bin_tally_topmeteors[sol_bin_mean]

            # Get cameras with the highest TAP
            bin_cams_toptap = bin_tally_toptap[sol_bin_mean]

            print()
            print("Sol = {:.4f} deg".format(sol_bin_mean))

            top_n_stations = 5
            print("Top {:d} by meteor number:".format(top_n_stations))
            for i, station_id in enumerate(bin_cams_topmeteors):
                station_data = bin_cams_topmeteors[station_id]
                n_meteors = station_data['meteors']
                tap = station_data['tap']/1e6
                print("    {:s}, {:5d} meteors, TAP = {:10.2f} km^2 h".format(station_id, n_meteors, tap))

                if i == top_n_stations - 1:
                    break

            print("Top {:d} by TAP:".format(top_n_stations))
            for i, station_id in enumerate(bin_cams_toptap):
                station_data = bin_cams_toptap[station_id]
                n_meteors = station_data['meteors']
                tap = station_data['tap']/1e6
                print("    {:s}, {:5d} meteors, TAP = {:10.2f} km^2 h".format(station_id, n_meteors, tap))

                if i == top_n_stations - 1:
                    break

        ###



    # If a CSV files was given, load the fluxes from the disk
    else:

        # get list of directories so that fixedfluxbin csv files can be found
        with open(fluxbatch_cml_args.batch_path) as f:
            # Parse the batch entries
            for line in f:
                line = line.replace("\n", "").replace("\r", "")

                if not len(line):
                    continue

                if line.startswith("#"):
                    continue

                flux_cml_args = fluxParser().parse_args(shlex.split(line, posix=0))
                shower_code = flux_cml_args.shower_code
                summary_population_index.append(calculatePopulationIndex(flux_cml_args.s))

        # Load data from single-station .csv file and plot it
        if fluxbatch_cml_args.single:
            dirname = os.path.dirname(fluxbatch_cml_args.batch_path)
            data1 = np.genfromtxt(
                os.path.join(dirname, fluxbatch_cml_args.output_filename + "_single.csv"),
                delimiter=',',
                dtype=None,
                encoding=None,
                skip_header=1,
            )

            station_list = []
            for stationID, sol, flux, lower, upper, _ in data1:
                plot_params = plot_info(stationID)

                ax[0].errorbar(
                    sol,
                    flux,
                    alpha=0.5,
                    capsize=5,
                    zorder=3,
                    linestyle='none',
                    yerr=[[flux - lower], [upper - flux]],
                    **plot_params
                )

        if os.path.exists(os.path.join(dirname, fluxbatch_cml_args.output_filename + "_combined.csv")):
            data2 = np.genfromtxt(
                os.path.join(dirname, fluxbatch_cml_args.output_filename + "_combined.csv"),
                delimiter=',',
                encoding=None,
                skip_header=1,
            )

            comb_sol_bins = data2[:, 0]
            comb_sol = data2[:-1, 1]
            comb_flux = data2[:-1, 2]
            comb_flux_lower = data2[:-1, 3]
            comb_flux_upper = data2[:-1, 4]
            comb_ta_prod = data2[:-1, 5]
            comb_num_meteors = data2[:-1, 6]
        else:
            comb_sol = []
            comb_sol_bins = []
            comb_flux = []
            comb_flux_lower = []
            comb_flux_upper = []
            comb_num_meteors = []
            comb_ta_prod = []


    # If data was able to be combined, plot the weighted flux
    if len(comb_sol):

        # Plotting weigthed flux
        ax[0].errorbar(
            comb_sol%360,
            comb_flux,
            yerr=[comb_flux - comb_flux_lower, comb_flux_upper - comb_flux],
            label="Weighted average flux at:\n" \
                + "LM = +6.5$^{\\mathrm{M}}$, " \
                + r"(${:s}$ g)".format(formatScientific(1000*mass_lim, 0)),
                #+ "$m_{\\mathrm{lim}} = $" + "${:s}$".format(formatScientific(1000*mass_lim, 0)) + " g (+6.5$^{\\mathrm{M}}$)",
            c='k',
            marker='o',
            linestyle='none',
            zorder=4,
        )

        # Plot the flux to the meteor LM
        ax[0].errorbar(
            comb_sol%360,
            comb_flux_lm_m,
            yerr=[comb_flux_lm_m - comb_flux_lm_m_lower, comb_flux_lm_m_upper - comb_flux_lm_m],
            label="Flux (1/{:.2f}x) at:\n".format(lm_m_to_6_5_factor) \
                + "LM = {:+.2f}".format(lm_m_mean) + "$^{\\mathrm{M}}$, " \
                + r"(${:s}$ g)".format(formatScientific(1000*mass_lim_lm_m_mean, 0)),
                #+ "$m_{\\mathrm{lim}} = $" + "${:s}$".format(formatScientific(1000*mass_lim_lm_m_mean, 0)) + " g ({:+.2f}".format(lm_m_mean) + "$^{\\mathrm{M}}$) ", \
            c='0.5',
            marker='o',
            linestyle='none',
            zorder=4,
        )

        # Set the minimum flux to 0
        ax[0].set_ylim(bottom=0)

        # Add the grid
        ax[0].grid(color='0.9')

        ax[0].legend()
        ax[0].set_title("{:s}, v = {:.1f} km/s, s = {:.2f}, r = {:.2f}".format(shower.name_full, 
            v_init/1000, calculateMassIndex(np.mean(summary_population_index)), 
            np.mean(summary_population_index)) 
                      # + ", $\\mathrm{m_{lim}} = $" + r"${:s}$ g ".format(formatScientific(1000*mass_lim, 0))
                      # + "at LM = +6.5$^{\\mathrm{M}}$"
                      )
        ax[0].set_ylabel("Flux (meteoroids / 1000 $\\cdot$ km$^2$ $\\cdot$ h)")


        ### Plot the ZHR on another axis ###

        # Create the right axis
        zhr_ax = ax[0].twinx()

        population_index = np.mean(summary_population_index)

        # Set the same range on the Y axis
        y_min, y_max = ax[0].get_ylim()
        zhr_min, zhr_max = calculateZHR([y_min, y_max], population_index)
        zhr_ax.set_ylim(zhr_min, zhr_max)

        # Get the flux ticks and set them to the zhr axis
        flux_ticks = ax[0].get_yticks()
        zhr_ax.set_yscale('segmented', points=calculateZHR(flux_ticks, population_index))

        zhr_ax.set_ylabel("ZHR at +6.5$^{\\mathrm{M}}$")

        ### ###


        if not fluxbatch_cml_args.onlyflux:


            ##### SUBPLOT 1 #####

            # Plot time-area product in the bottom plot
            plot1 = ax[1].bar(
                ((comb_sol_bins[1:] + comb_sol_bins[:-1])/2)%360,
                comb_ta_prod/1e9,
                comb_sol_bins[1:] - comb_sol_bins[:-1],
                label='Time-area product (TAP)',
                color='0.65',
                edgecolor='0.55'
            )

            # Plot the minimum time-area product as a horizontal line
            ax[1].hlines(
                min_tap,
                np.min(comb_sol%360),
                np.max(comb_sol%360),
                colors='k',
                linestyles='solid',
                label="Min. TAP",
            )

            ax[1].set_ylabel("TAP (1000 $\\cdot$ km$^2$ $\\cdot$ h)")


            # Plot the number of meteors on the right axis
            side_ax = ax[1].twinx()
            plot2 = side_ax.scatter(comb_sol%360, comb_num_meteors, c='k', label='Meteors', s=8)

            # Plot the minimum meteors line
            side_ax.hlines(
                min_meteors,
                np.min(comb_sol%360),
                np.max(comb_sol%360),
                colors='k',
                linestyles='--',
                label="Min. meteors"
            )
            side_ax.set_ylabel('Num meteors')
            side_ax.set_ylim(bottom=0)


            # Add a combined legend
            lines, labels = ax[1].get_legend_handles_labels()
            lines2, labels2 = side_ax.get_legend_handles_labels()
            side_ax.legend(lines + lines2, labels + labels2)


            ##### SUBPLOT 2 #####

            # Plot the radiant elevation
            ax[2].scatter(comb_sol%360, comb_rad_elev, label="Rad. elev. (TAP-weighted)", color='0.75', s=15, marker='s')

            # Plot the radiant distance
            ax[2].scatter(comb_sol%360, comb_rad_dist, label="Rad. dist.", color='0.25', s=20, marker='x')

            ax[2].set_ylabel("Angle (deg)")

            ### Plot lunar phases per year ###

            moon_ax = ax[2].twinx()

            # Set line plot cycler
            line_cycler   = (cycler(color=["#E69F00", "#56B4E9", "#009E73", "#0072B2", "#D55E00", "#CC79A7", "#F0E442"]) +
                     cycler(linestyle=["-", "--", "-.", ":", "-", "--", "-."]))

            moon_ax.set_prop_cycle(line_cycler)


            # Set up observer
            o = ephem.Observer()
            o.lat = str(0)
            o.long = str(0)
            o.elevation = 0
            o.horizon = '0:0'

            for dt_range, dt_arr in bin_datetime_yearly:

                dt_bin_beg, dt_bin_end = dt_range
                dt_mid = jd2Date((datetime2JD(dt_bin_beg) + datetime2JD(dt_bin_end))/2, dt_obj=True)

                sol_moon_phase = []
                moon_phases = []

                for dt in dt_arr:

                    o.date = dt
                    m = ephem.Moon()
                    m.compute(o)

                    moon_phases.append(m.phase)

                # Plot Moon phases
                moon_ax.plot(np.degrees(sol_bins), moon_phases, label="{:d} moon phase".format(dt_mid.year))

            moon_ax.set_ylabel("Moon phase")
            moon_ax.set_ylim([0, 100])

            # Add a combined legend
            lines, labels = ax[2].get_legend_handles_labels()
            lines2, labels2 = moon_ax.get_legend_handles_labels()
            moon_ax.legend(lines + lines2, labels + labels2)

            ### ###


            ##### SUBPLOT 3 #####

            ### Plot the TAP-weighted limiting magnitude ###

            lm_ax = ax[3].twinx()

            lm_ax.scatter(comb_sol%360, comb_lm_m, label="Meteor LM", color='0.5', s=20)

            lm_ax.invert_yaxis()
            lm_ax.set_ylabel("Meteor LM")
            #lm_ax.legend()

            # Add one magnitude of buffer to every end, round to 0.5
            lm_min, lm_max = lm_ax.get_ylim()
            lm_ax.set_ylim(np.ceil(2*(lm_min))/2, np.floor(2*(lm_max))/2)


            # Plot the TAP-weighted meteor LM

            lm_ax.hlines(
                lm_m_mean,
                np.min(comb_sol%360),
                np.max(comb_sol%360),
                colors='k',
                alpha=0.5,
                linestyles='dashed',
                label="Mean meteor LM = {:+.2f}".format(lm_m_mean) + "$^{\\mathrm{M}}$",
            )


            ###

            
            # Plot the angular velocity
            ax[3].scatter(comb_sol%360, comb_ang_vel, label="Angular velocity", color='0.0', s=30, marker='+')
            ax[3].set_ylabel("Ang. vel. (deg/s)")


            # Add a combined legend
            lines, labels = ax[3].get_legend_handles_labels()
            lines2, labels2 = lm_ax.get_legend_handles_labels()
            lm_ax.legend(lines + lines2, labels + labels2)


        ax[subplot_rows - 1].set_xlabel("Solar longitude (deg)")





    # Show plot
    
    plt.tight_layout()

    fig_path = os.path.join(dir_path, fluxbatch_cml_args.output_filename + ".png")
    print("Figure saved to:", fig_path)
    plt.savefig(fig_path, dpi=300)

    plt.show()


    # Write the computed weigthed flux to disk
    if not fluxbatch_cml_args.csv:

        if len(comb_sol):

            data_out_path = os.path.join(dir_path, fluxbatch_cml_args.output_filename + "_combined.csv")
            with open(data_out_path, 'w') as fout:
                fout.write("# Shower parameters:\n")
                fout.write("# Shower         = {:s}\n".format(shower_code))
                fout.write("# r              = {:.2f}\n".format(population_index_mean))
                fout.write("# s              = {:.2f}\n".format(calculateMassIndex(population_index_mean)))
                fout.write("# m_lim @ +6.5M  = {:.2e} kg\n".format(mass_lim))
                fout.write("# Met LM mean    = {:.2e}\n".format(lm_m_mean))
                fout.write("# m_lim @ {:+.2f}M = {:.2e} kg\n".format(lm_m_mean, mass_lim_lm_m_mean))
                fout.write("# CI int.        = {:.1f} %\n".format(100*ci))
                fout.write("# Binning parameters:\n")
                fout.write("# Min. meteors     = {:d}\n".format(min_meteors))
                fout.write("# Min TAP          = {:.2f} x 1000 km^2 h\n".format(min_tap))
                fout.write("# Min bin duration = {:.2f} h\n".format(min_bin_duration))
                fout.write("# Max bin duration = {:.2f} h\n".format(max_bin_duration))
                fout.write(
                    "# Sol bin start (deg), Mean Sol (deg), Flux@+6.5M (met / 1000 km^2 h), Flux CI low, Flux CI high, Flux@+{:.2f}M (met / 1000 km^2 h), Flux CI low, Flux CI high, ZHR, ZHR CI low, ZHR CI high, Meteor Count, Time-area product (corrected to +6.5M) (1000 km^2/h), Meteor LM, Radiant elev (deg), Radiat dist (deg), Ang vel (deg/s)\n".format(lm_m_mean)
                )
                for (_sol_bin_start,
                    _mean_sol,
                    _flux,
                    _flux_lower,
                    _flux_upper,
                    _flux_lm,
                    _flux_lm_lower,
                    _flux_lm_upper,
                    _zhr,
                    _zhr_lower,
                    _zhr_upper,
                    _nmeteors,
                    _tap,
                    _lm_m,
                    _rad_elev,
                    _rad_dist,
                    _ang_vel) \
                in zip(
                        comb_sol_bins,
                        comb_sol,
                        comb_flux,
                        comb_flux_lower,
                        comb_flux_upper,
                        comb_flux_lm_m,
                        comb_flux_lm_m_lower,
                        comb_flux_lm_m_upper,
                        comb_zhr,
                        comb_zhr_lower,
                        comb_zhr_upper,
                        comb_num_meteors,
                        comb_ta_prod,
                        comb_lm_m,
                        comb_rad_elev,
                        comb_rad_dist,
                        comb_ang_vel
                        ):

                    fout.write(
                        "{:.8f},{:.8f},{:.3f},{:.3f},{:.3f},{:.3f},{:.3f},{:.3f},{:.3f},{:.3f},{:.3f},{:d},{:.3f},{:.2f},{:.2f},{:.2f},{:.2f}\n".format(
                        _sol_bin_start,
                        _mean_sol,
                        _flux,
                        _flux_lower,
                        _flux_upper,
                        _flux_lm,
                        _flux_lm_lower,
                        _flux_lm_upper,
                        _zhr,
                        _zhr_lower,
                        _zhr_upper,
                        int(_nmeteors),
                        _tap/1e9,
                        _lm_m,
                        _rad_elev,
                        _rad_dist,
                        _ang_vel,
                        ))

                fout.write("{:.8f},,,,,,,,,,,,,,,,\n".format(comb_sol_bins[-1]))

            print("Data saved to:", data_out_path)



        # Save the single-station fluxes
        if fluxbatch_cml_args.single:

            data_out_path = os.path.join(dir_path, fluxbatch_cml_args.output_filename + "_single.csv")
            with open(data_out_path, 'w') as fout:
                fout.write(
                    "# Station, Sol (deg), Flux@+6.5M (met/1000km^2/h), Flux lower bound, Flux upper bound, Population Index\n"
                )
                for entry in output_data:
                    print(entry)
                    stationID, sol, flux, lower, upper, population_index = entry

                    fout.write(
                        "{:s},{:.8f},{:.3f},{:.3f},{:.3f},{}\n".format(
                            stationID, sol, flux, lower, upper, population_index
                        )
                    )
            print("Data saved to:", data_out_path)
