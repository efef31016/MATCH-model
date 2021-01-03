# Copyright (c) 2016-2017 The Switch Authors. All rights reserved.
# Modifications copyright (c) 2021 Gregory J. Miller. All rights reserved.
# Licensed under the Apache License, Version 2.0, which is in the LICENSE file.

"""
This module defines storage technologies. It builds on top of generic
generators, adding components for deciding how much energy to build into
storage, when to charge, energy accounting, etc.
"""

from pyomo.environ import *
import os, collections

dependencies = 'switch_model.timescales', 'switch_model.balancing.load_zones',\
    'switch_model.financials', 'switch_model.energy_sources.properties', \
    'switch_model.generators.core.build', 'switch_model.generators.core.dispatch', 'switch_model.generators.core.no_commit'

def define_components(mod):
    """

    STORAGE_GENS is the subset of projects that can provide energy storage.

    HYBRID_STORAGE_GENS is the subset of STORAGE_GENS that are paired with a generation project as a hybrid resource

    STORAGE_GEN_BLD_YRS is the subset of GEN_BLD_YRS, restricted
    to storage projects.

    storage_roundtrip_efficiency[STORAGE_GENS] describes the round trip
    efficiency of a storage technology. A storage technology that is 75
    percent efficient would have a storage_efficiency of .75. If 1 MWh
    was stored in such a storage project, 750 kWh would be available for
    extraction later. Internal leakage or energy dissipation of storage
    technologies is assumed to be neglible, which is consistent with
    short-duration storage technologies currently on the market which
    tend to consume stored power within 1 day. If a given storage
    technology has significant internal discharge when it stores power
    for extended time perios, then those behaviors will need to be
    modeled in more detail.

    storage_charge_to_discharge_ratio[STORAGE_GENS] describes the maximum rate
    that energy can be stored, expressed as a ratio of discharge power
    capacity. This is an optional parameter and will default to 1. If a
    storage project has 1 MW of dischage capacity and a storage_charge_to_discharge_ratio
    of 1.2, then it can consume up to 1.2 MW of power while charging.

    storage_energy_to_power_ratio[STORAGE_GENS], if specified, restricts
    the storage capacity (in MWh) to be a fixed multiple of the output
    power (in MW), i.e., specifies a particular number of hours of
    storage capacity. Omit this column or specify "." to allow Switch
    to choose the energy/power ratio. (Note: gen_storage_energy_overnight_cost
    or gen_overnight_cost should often be set to 0 when using this.)

    storage_max_annual_cycles[STORAGE_GENS], if specified, restricts
    the number of charge/discharge cycles each storage project can perform
    per year; one cycle is defined as discharging an amount of energy
    equal to the storage capacity of the project.

    gen_storage_energy_overnight_cost[(g, bld_yr) in
    STORAGE_GEN_BLD_YRS] is the overnight capital cost per MWh of
    energy capacity for building the given storage technology installed in the
    given investment period. This is only defined for storage technologies.
    Note that this describes the energy component and the overnight_cost
    describes the power component.

    storage_hybrid_generation_project[g], if specified, represents the name
    of the generation project with which this storage asset is paired as a hybrid resource.

    BuildStorageEnergy[(g, bld_yr) in STORAGE_GEN_BLD_YRS]
    is a decision of how much energy capacity to build onto a storage
    project. This is analogous to BuildGen, but for energy rather than power.

    StorageEnergyInstallCosts[PERIODS] is an expression of the
    annual costs incurred by the BuildStorageEnergy decision.

    StorageEnergyCapacity[g, period] is an expression describing the
    cumulative available energy capacity of BuildStorageEnergy. This is
    analogous to GenCapacity.

    STORAGE_GEN_TPS is the subset of GEN_TPS,
    restricted to storage projects.

    ChargeStorage[(g, t) in STORAGE_GEN_TPS] is a dispatch
    decision of how much to charge a storage project in each timepoint.

    ZoneTotalStorageCharge[LOAD_ZONE, TIMEPOINT] is an expression describing the
    aggregate impact of ChargeStorage in each load zone and timepoint.

    Charge_Storage_Upper_Limit[(g, t) in STORAGE_GEN_TPS]
    constrains ChargeStorage to available power capacity (accounting for
    storage_charge_to_discharge_ratio)

    Charge_Hybrid_Storage_Upper_Limit[(g, t) in HYBRID_STORAGE_GEN_TPS]
    constrains ChargeStorage to be no greater than the dispatch of its 
    paired generator in each timepoint

    StateOfCharge[(g, t) in STORAGE_GEN_TPS] is a variable
    for tracking state of charge. This value stores the state of charge at
    the end of each timepoint for each storage project.

    Track_State_Of_Charge[(g, t) in STORAGE_GEN_TPS] constrains
    StateOfCharge based on the StateOfCharge in the previous timepoint,
    ChargeStorage and DispatchGen.

    State_Of_Charge_Upper_Limit[(g, t) in STORAGE_GEN_TPS]
    constrains StateOfCharge based on installed energy capacity.

    """

    # DEFINE SETS
    #############

    
    mod.HYBRID_STORAGE_GENS = Set(within=mod.STORAGE_GENS)
    mod.STORAGE_GEN_PERIODS = Set(
        within=mod.GEN_PERIODS,
        initialize=lambda m: [(g, p) for g in m.STORAGE_GENS for p in m.PERIODS_FOR_GEN[g]]
    )

    mod.STORAGE_GEN_TPS = Set(
        dimen=2,
        initialize=lambda m: (
            (g, tp)
                for g in m.STORAGE_GENS
                    for tp in m.TPS_FOR_GEN[g]))

    mod.HYBRID_STORAGE_GEN_TPS = Set(
        dimen=2,
        initialize=lambda m: (
            (g, tp)
                for g in m.HYBRID_STORAGE_GENS
                    for tp in m.TPS_FOR_GEN[g]))

    mod.STORAGE_GENS_IN_ZONE = Set(
        mod.LOAD_ZONES,
        initialize=lambda m, z: [g for g in m.GENS_IN_ZONE[z] if m.gen_is_storage[g]])

    # TODO: build this set up instead of filtering down, to improve performance
    mod.STORAGE_GEN_BLD_YRS = Set(
        dimen=2,
        initialize=mod.GEN_BLD_YRS,
        filter=lambda m, g, bld_yr: g in m.STORAGE_GENS)

    

    def period_active_gen_rule(m, period):
        if not hasattr(m, 'period_active_gen_dict'):
            m.period_active_gen_dict = collections.defaultdict(set)
            for (_g, _period) in m.STORAGE_GEN_PERIODS:
                m.period_active_gen_dict[_period].add(_g)
        result = m.period_active_gen_dict.pop(period)
        if len(m.period_active_gen_dict) == 0:
            delattr(m, 'period_active_gen_dict')
        return result
    mod.STORAGE_GENS_IN_PERIOD = Set(mod.PERIODS, initialize=period_active_gen_rule,
        doc="The set of projects active in a given period.")

    # DEFINE PARAMETERS
    ###################

    mod.storage_roundtrip_efficiency = Param(
        mod.STORAGE_GENS,
        within=PercentFraction)
    # TODO: rename to gen_charge_to_discharge_ratio?
    mod.storage_charge_to_discharge_ratio = Param(
        mod.STORAGE_GENS,
        within=NonNegativeReals,
        default=1.0)
    mod.storage_energy_to_power_ratio = Param(
        mod.STORAGE_GENS,
        within=NonNegativeReals,
        default=float("inf")) # inf is a flag that no value is specified (nan and None don't work)
    mod.storage_max_annual_cycles = Param(
        mod.STORAGE_GENS,
        within=NonNegativeReals,
        default=float('inf'))
    mod.storage_hybrid_generation_project = Param(
        mod.HYBRID_STORAGE_GENS,
        validate= lambda m,val,g: val in m.GENERATION_PROJECTS and val not in m.STORAGE_GENS) #validate the paired generator is in the generator list and isnt another storage project
    mod.storage_hybrid_capacity_ratio = Param(
        mod.STORAGE_GENS,
        within=NonNegativeReals,
        default=float("inf"))
    mod.storage_leakage_loss = Param(
        mod.STORAGE_GENS,
        within=PercentFraction,
        default=0.0)
    
    # STORAGE BUILD DECISIONS
    #########################

    mod.BuildStorageEnergy = Var(
        mod.STORAGE_GEN_BLD_YRS,
        within=NonNegativeReals)

    # TODO: add a condition that hybrid storage and paired generator must be built together
    #if generator is built, storage must be built

    mod.Enforce_Hybrid_Build = Constraint(
        mod.STORAGE_GEN_BLD_YRS,
        rule=lambda m, g, y: 
        Constraint.Skip if m.storage_hybrid_capacity_ratio[g] == float("inf") # no value specified
        else
        (m.BuildGen[g, y] == m.storage_hybrid_capacity_ratio[g] * m.BuildGen[m.storage_hybrid_generation_project[g], y])
    )

    mod.StorageEnergyCapacity = Expression(
        mod.STORAGE_GENS, mod.PERIODS,
        rule=lambda m, g, period: sum(
            m.BuildStorageEnergy[g, bld_yr]
            for bld_yr in m.BLD_YRS_FOR_GEN_PERIOD[g, period]))

    # use fixed energy/power ratio (# hours of capacity) when specified
    mod.Enforce_Fixed_Energy_Storage_Ratio = Constraint(
        mod.STORAGE_GEN_BLD_YRS,
        rule=lambda m, g, y:
            Constraint.Skip if m.storage_energy_to_power_ratio[g] == float("inf") # no value specified
            else
            (m.BuildStorageEnergy[g, y] == m.storage_energy_to_power_ratio[g] * m.BuildGen[g, y])
    )

    # NOTE: Storage capacity costs are added to the objective function in the build.py module

 
    # STORAGE DISPATCH
    ########################

    mod.ChargeStorage = Var(
        mod.STORAGE_GEN_TPS,
        within=NonNegativeReals)

    mod.DispatchStorage = Var(
            mod.STORAGE_GEN_TPS,
            within=NonNegativeReals)

    mod.Enforce_Storage_Dispatch_Upper_Limit = Constraint(
        mod.STORAGE_GEN_TPS,
        rule=lambda m, g, t: (
            m.DispatchStorage[g, t] <= m.DispatchUpperLimit[g, t]))

    mod.Enforce_Storage_Charge_Upper_Limit = Constraint(
        mod.STORAGE_GEN_TPS,
        rule=lambda m, g, t: (
            m.ChargeStorage[g,t] <= m.DispatchUpperLimit[g, t] * m.storage_charge_to_discharge_ratio[g]))

    # Summarize storage charging for the energy balance equations
    mod.ZoneTotalStorageDispatch = Expression(
        mod.ZONE_TIMEPOINTS,
        rule=lambda m, z, t: \
            sum(m.DispatchStorage[g, t]
                for g in m.STORAGE_GENS_IN_ZONE[z]
                if (g, t) in m.STORAGE_GEN_TPS),
    )
    mod.Zone_Power_Injections.append('ZoneTotalStorageDispatch')

    """
    mod.ZoneTotalStorageCharge = Expression(
        mod.ZONE_TIMEPOINTS,
        rule=lambda m, z, t: \
            sum(m.ChargeStorage[g, t]
                for g in m.STORAGE_GENS_IN_ZONE[z]
                if (g, t) in m.STORAGE_GEN_TPS),
    )
    #mod.Zone_Power_Withdrawals.append('ZoneTotalStorageCharge')
    """

    # NOTE: This was the old implementation to track chargestorage, but it seems more complicated
    # than using the implementation below, which is adapted from how DispatchGen is summed
    def rule(m, z, t):
        # Construct and cache a set for summation as needed
        if not hasattr(m, 'Storage_Charge_Summation_dict'):
            m.Storage_Charge_Summation_dict = collections.defaultdict(set)
            for g, t2 in m.STORAGE_GEN_TPS:
                z2 = m.gen_load_zone[g]
                m.Storage_Charge_Summation_dict[z2, t2].add(g)
        # Use pop to free memory
        relevant_projects = m.Storage_Charge_Summation_dict.pop((z, t), {})
        return sum(m.ChargeStorage[g, t] for g in relevant_projects)
    mod.ZoneTotalStorageCharge = Expression(mod.ZONE_TIMEPOINTS, rule=rule)
    
    # Register net charging with zonal energy balance. Discharging is already
    # covered by DispatchGen.
    #mod.Zone_Power_Withdrawals.append('ZoneTotalStorageCharge')


    # Zonal Charging should be less than ExcessGen. this requires storage to charge from any 
    # generation that is not dispatched to meet load. This also prevents storage from charging
    # from other storage dispatch since excessgen only applies to generators
    # NOTE: This will mean that storage cannot charge from system power

    mod.Zonal_Charge_Storage_Upper_Limit = Constraint(
        mod.ZONE_TIMEPOINTS,
        rule = lambda m, z, t: m.ZoneTotalStorageCharge[z,t] <= m.ZoneTotalExcessGen[z,t] 
    )

    # HYBRID STORAGE CHARGING 
    #########################

    mod.Charge_Hybrid_Storage_Upper_Limit = Constraint(
        mod.HYBRID_STORAGE_GEN_TPS,
        rule=lambda m, g, t: m.ChargeStorage[g,t] <= m.ExcessGen[m.storage_hybrid_generation_project[g],t])

    # Because the bus of a hybrid generator is likely sized to the nameplate capacity of the generator portion of the project
    # the total combined dispatch from the storage portion and the generator portion should not be allowed to exceed that 
    # nameplate capacity. For example, a 100MW solar + 50MW storage hybrid project should only be allowed to dispatch 
    # a combined total of 100MW in any timepoint.
    mod.Hybrid_Dispatch_Limit = Constraint(
        mod.HYBRID_STORAGE_GEN_TPS,
        rule=lambda m, g, t: m.DispatchStorage[g,t] + m.DispatchUpperLimit[m.storage_hybrid_generation_project[g], t] <= m.GenCapacityInTP[m.storage_hybrid_generation_project[g],t])

    #STATE OF CHARGE
    ################
    mod.StateOfCharge = Var(
        mod.STORAGE_GEN_TPS,
        within=NonNegativeReals)

    def Track_State_Of_Charge_rule(m, g, t):
        return m.StateOfCharge[g, t] == \
            m.StateOfCharge[g, m.tp_previous[t]] - \
            (m.StateOfCharge[g, m.tp_previous[t]] * m.storage_leakage_loss[g]) + \
            ((m.ChargeStorage[g, t] * sqrt(m.storage_roundtrip_efficiency[g])) -
            (m.DispatchStorage[g, t] / sqrt(m.storage_roundtrip_efficiency[g]))) * m.tp_duration_hrs[t]
    mod.Track_State_Of_Charge = Constraint(
        mod.STORAGE_GEN_TPS,
        rule=Track_State_Of_Charge_rule)

    def State_Of_Charge_Upper_Limit_rule(m, g, t):
        return m.StateOfCharge[g, t] <= \
            m.StorageEnergyCapacity[g, m.tp_period[t]]
    mod.State_Of_Charge_Upper_Limit = Constraint(
        mod.STORAGE_GEN_TPS,
        rule=State_Of_Charge_Upper_Limit_rule)

    # CYCLE LIMITS
    ##############
    mod.Battery_Cycle_Count = Expression(
        mod.STORAGE_GEN_PERIODS,
        rule=lambda m, g, p: sum(m.DispatchStorage[g, t] / sqrt(m.storage_roundtrip_efficiency[g]) * m.tp_duration_hrs[t] for t in m.TPS_IN_PERIOD[p]))
    
    # batteries can only complete the specified number of cycles per year, averaged over each period
    mod.Battery_Cycle_Limit = Constraint(
        mod.STORAGE_GEN_PERIODS,
        rule=lambda m, g, p:
            # solvers sometimes perform badly with infinite constraint
            Constraint.Skip if m.storage_max_annual_cycles[g] == float('inf')
            else (m.Battery_Cycle_Count[g,p]
                <=
                m.storage_max_annual_cycles[g] * m.StorageEnergyCapacity[g, p] * m.period_length_years[p]
            )
    )

    # ENERGY ARBITRAGE COST/REVENUE RULES
    #####################################

    mod.StorageDispatchPnodeCost = Expression(
        mod.STORAGE_GEN_TPS,
        rule = lambda m, g, t: (m.ChargeStorage[g, t] - m.DispatchStorage[g, t]) * m.nodal_price[m.gen_pricing_node[g], t]
    )
    mod.StorageDispatchPnodeCostInTP = Expression(
        mod.TIMEPOINTS,
        rule = lambda m, t: sum(m.StorageDispatchPnodeCost[g, t] for g in m.STORAGE_GENS)
    )
    mod.Cost_Components_Per_TP.append('StorageDispatchPnodeCostInTP')

    # A hybrid generator should not pay the PPA cost of energy generated but stored, since this energy never crosses
    # the bus, so we want to discount ExcessGenCostInTP by the amount charged; however, the storage should pay the PPA
    # cost when dispatching because the energy will cross the generator bus
    mod.HybridChargePPADiscountInTP = Expression(
        mod.TIMEPOINTS,
        rule=lambda m, t: - sum(
            m.ChargeStorage[g, t] * m.ppa_energy_cost[m.storage_hybrid_generation_project[g]]
            for g in m.GENS_IN_PERIOD[m.tp_period[t]]
            if g in m.HYBRID_STORAGE_GENS),
        doc="Summarize costs for the objective function")
    mod.Cost_Components_Per_TP.append('HybridChargePPADiscountInTP')

    mod.HybridStoragePPACostInTP = Expression(
        mod.TIMEPOINTS,
        rule=lambda m, t: sum(
            m.DispatchStorage[g, t] * m.ppa_energy_cost[m.storage_hybrid_generation_project[g]]
            for g in m.GENS_IN_PERIOD[m.tp_period[t]]
            if g in m.HYBRID_STORAGE_GENS),
        doc="Summarize costs for the objective function")
    mod.Cost_Components_Per_TP.append('HybridStoragePPACostInTP')


def load_inputs(mod, switch_data, inputs_dir):
    """

    Import storage parameters. Optional columns are noted with a *.

    generation_projects_info.csv
        GENERATION_PROJECT, ...
        storage_roundtrip_efficiency, storage_charge_to_discharge_ratio*,
        storage_energy_to_power_ratio*, storage_max_annual_cycles*

    gen_build_costs.csv
        GENERATION_PROJECT, build_year, ...
        gen_storage_energy_overnight_cost

    """

    # TODO: maybe move these columns to a storage_gen_info file to avoid the weird index
    # reading and avoid having to create these extra columns for all projects;
    # Alternatively, say that these values are specified for _all_ projects (maybe with None
    # as default) and then define STORAGE_GENS as the subset of projects for which
    # storage_roundtrip_efficiency has been specified, then require valid settings for all
    # STORAGE_GENS.
    switch_data.load_aug(
        filename=os.path.join(inputs_dir, 'generation_projects_info.csv'),
        auto_select=True,
        optional_params=['storage_charge_to_discharge_ratio', 'storage_energy_to_power_ratio', 'storage_max_annual_cycles'],
        param=(mod.storage_roundtrip_efficiency, mod.storage_charge_to_discharge_ratio, mod.storage_energy_to_power_ratio, mod.storage_max_annual_cycles, mod.storage_hybrid_generation_project, mod.storage_hybrid_capacity_ratio, mod.storage_leakage_loss))
    
    
    
    # Base the set of storage projects on storage efficiency being specified.
    # TODO: define this in a more normal way
    switch_data.data()['HYBRID_STORAGE_GENS'] = {
        None: list(switch_data.data(name='storage_hybrid_generation_project').keys())}



def post_solve(instance, outdir):
    """
    Export storage build information to storage_builds.csv, and storage
    dispatch info to storage_dispatch.csv
    """
    import switch_model.reporting as reporting
    reporting.write_table(
        instance, instance.STORAGE_GEN_BLD_YRS,
        output_file=os.path.join(outdir, "storage_builds.csv"),
        headings=("generation_project", "period", "load_zone",
                  "IncrementalPowerCapacityMW", "IncrementalEnergyCapacityMWh",
                  "OnlinePowerCapacityMW", "OnlineEnergyCapacityMWh" ),
        values=lambda m, g, bld_yr: (
            g, bld_yr, m.gen_load_zone[g],
            m.BuildGen[g, bld_yr], m.BuildStorageEnergy[g, bld_yr],
            m.GenCapacity[g, bld_yr], m.StorageEnergyCapacity[g, bld_yr]
            ))
    reporting.write_table(
        instance, instance.STORAGE_GEN_TPS,
        output_file=os.path.join(outdir, "storage_dispatch.csv"),
        headings=("generation_project", "timestamp", "load_zone",
                  "ChargeMW", 'DispatchMW', 
                  "StateOfCharge", "StorageDispatchPnodeCost",),
        values=lambda m, g, t: (
            g, m.tp_timestamp[t], m.gen_load_zone[g],
            m.ChargeStorage[g, t], m.DispatchStorage[g, t], 
            m.StateOfCharge[g, t], m.StorageDispatchPnodeCost[g, t]
            ))
    reporting.write_table(
        instance, instance.STORAGE_GENS, instance.PERIODS,
        output_file=os.path.join(outdir, "storage_cycle_count.csv"),
        headings=("generation_project", "period", "storage_max_annual_cycles", "Battery_Cycle_Count"),
        values=lambda m, g, p: (
            g, p, m.storage_max_annual_cycles[g], m.Battery_Cycle_Count[g, p]
            ))
