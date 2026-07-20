'use client'

import { Fragment, useCallback, useEffect, useState } from 'react';
import { InputSection } from '../../../InputSection';
//import { formList } from '@mantine/form';
import { HyperparametersCollection, HyperparameterTypes, IntegerHyperparameter } from '../../../../../lib/db_types';
import { useDebounce } from "use-debounce";
import { final } from 'pino';

/**
 * Statistics for a single hyperparameter's expanded value set.
 *   n = total number of values the parameter expands to
 *   e = how many of those values equal the parameter's default
 *       (the number of values that "count as unchanged" in the OAT filter)
 * `n - e` is therefore the number of values that deviate from the default.
 *
 * These mirror the runner's `expand_values` in apps/runner/modules/configs.py
 * exactly, so the number shown here matches the configs actually generated.
 */
interface ParamValueStats {
    n: number;
    e: number;
}

/** Mirror of the runner's `get_decimal_places`. */
function getDecimalPlaces(num: number): number {
    if (Number.isInteger(num)) {
        return 0;
    }
    const parts = String(num).split('.');
    return parts.length > 1 ? parts[1].length : 0;
}

/**
 * Faithful port of the runner's `float_range` generator, including its
 * repeated `current += step` accumulation and per-value rounding. Matching the
 * accumulation (rather than using a closed-form `(max-min)/step`) is what keeps
 * the count in sync with Python for step sizes that don't divide the range
 * cleanly.
 */
function floatRange(start: number, stop: number, step: number, decimals: number): number[] {
    const factor = Math.pow(10, decimals);
    const round = (x: number) => Math.round(x * factor) / factor;
    const out: number[] = [];
    if (step <= 0) {
        return out;
    }
    let current = start;
    // Guard against pathological/in-progress inputs producing an unbounded loop.
    let guard = 0;
    while (round(current) < round(stop) && guard < 10_000_000) {
        out.push(round(current));
        current += step;
        guard++;
    }
    return out;
}

/**
 * Number of values in a param group column (each column has the same length).
 */
function getParamGroupRange(param: any): number {
    const values = Object.values((param.values || {}) as any[][]);
    return values.length > 0 ? values[0].length : 0;
}

/** Compute {n, e} for a normal (non-paramgroup) hyperparameter. */
function getParamValueStats(param: any): ParamValueStats {
    switch (param.type) {
        case HyperparameterTypes.INTEGER: {
            const { min, max, step } = param;
            if (!(step > 0) || max < min) {
                return { n: 1, e: 1 };
            }
            const n = Math.floor((max - min) / step) + 1;
            const def = param.default;
            const reachable = def >= min && def <= max && (def - min) % step === 0;
            return { n, e: reachable ? 1 : 0 };
        }
        case HyperparameterTypes.FLOAT: {
            const { min, max, step } = param;
            if (!(step > 0) || max < min) {
                return { n: 1, e: 1 };
            }
            const decimals = Math.max(
                getDecimalPlaces(min),
                getDecimalPlaces(max),
                getDecimalPlaces(step),
            );
            const values = floatRange(min, max + step, step, decimals);
            const factor = Math.pow(10, decimals);
            const def = Math.round(param.default * factor) / factor;
            const e = values.reduce((acc, v) => acc + (v === def ? 1 : 0), 0);
            return { n: values.length, e };
        }
        case HyperparameterTypes.BOOLEAN: {
            // Values are [true, false]; a boolean default always matches one of them.
            const def = param.default;
            return { n: 2, e: def === true || def === false ? 1 : 0 };
        }
        case HyperparameterTypes.STRING_LIST: {
            const values: string[] = param.values || [];
            const e = values.reduce((acc, v) => acc + (v === param.default ? 1 : 0), 0);
            return { n: values.length, e };
        }
        case HyperparameterTypes.STRING:
        default:
            // Strings are treated as constants by the runner (single value).
            return { n: 1, e: 1 };
    }
}

function calcPermutations(parameters: HyperparametersCollection): number {
    const params = parameters.hyperparameters;

    const paramGroups = params.filter(p => p.type === HyperparameterTypes.PARAM_GROUP);
    const normalParams = params.filter(p => p.type !== HyperparameterTypes.PARAM_GROUP);

    // Matches the runner's filter, which keys purely off the default sentinel
    // (it does NOT consult `useDefault` — that field is unused when counting).
    const hasValidDefault = (p: any): boolean => {
        const def = p.default;
        return def !== -1 && def !== "-1" && def !== '' && def !== undefined && def !== null;
    };

    const constrained = normalParams.filter(hasValidDefault);
    const free = normalParams.filter(p => !hasValidDefault(p));

    // 1. Free parameters vary independently: the full cartesian product.
    let freeProduct = 1;
    for (const p of free) {
        freeProduct *= getParamValueStats(p).n;
    }

    // 2. Constrained parameters use one-at-a-time deviation from their default:
    //    a permutation is kept only if at most one constrained param differs
    //    from its default. With e_i values equal to the default and dev_i that
    //    deviate, the count is:
    //        prod(e_i)  +  sum_j [ dev_j * prod_{i != j} e_i ]
    //    i.e. the all-defaults baseline plus, for each param, its deviations
    //    while every other constrained param sits at its default. This reduces
    //    to 1 + sum(n_i - 1) only when every default is actually reachable
    //    (e_i == 1); it stays correct when a default falls off the step grid.
    const stats = constrained.map(getParamValueStats);
    let constrainedCombinations: number;
    if (stats.length === 0) {
        constrainedCombinations = 1;
    } else {
        const prodE = stats.reduce((acc, s) => acc * s.e, 1);
        let sumTerm = 0;
        for (let j = 0; j < stats.length; j++) {
            let others = 1;
            for (let i = 0; i < stats.length; i++) {
                if (i !== j) {
                    others *= stats[i].e;
                }
            }
            sumTerm += (stats[j].n - stats[j].e) * others;
        }
        constrainedCombinations = prodE + sumTerm;
    }

    let total = freeProduct * constrainedCombinations;

    // 3. Handle Param Groups (sum of group lengths multiplied by current total).
    if (paramGroups.length > 0) {
        let groupTotal = 0;
        for (const pg of paramGroups) {
            groupTotal += getParamGroupRange(pg);
        }
        total *= groupTotal;
    }

    return total;
}

export const ParameterOptions = ['integer', 'float', 'bool', 'stringlist', 'paramgroup'] as const;

export const ParamStep = ({ form, confirmedValues, setConfirmedValues, ...props }) => {

	const [text, setText] = useState('');
	const [permutations, setPermutations] = useState(0);
	const [debouncedFormValues] = useDebounce(form.values, 300);

	useEffect(() => {
		const permutations = calcPermutations(debouncedFormValues);
		setText(permutations !== undefined ? permutations.toString() : 'Permutations Unable to be Calculated');
		setPermutations(permutations ?? 0);
	}, [debouncedFormValues, confirmedValues]);

	return (
		<div className='h-full flex flex-col space-y-6 py-6 sm:space-y-0 sm:divide-y sm:divide-gray-200 sm:py-0'>
			<Fragment>
				<InputSection header={'Parameters'}>
					<div className='sm:col-span-4 inline-flex'>
						<span className='rounded-l-md text-sm text-white font-bold bg-blue-600  items-center px-4 py-2 border border-transparent'>
							+
						</span>
						<span className='relative z-0 inline-flex flex-1 shadow-sm rounded-md'>
							{ParameterOptions.map((type) => (
								<button
									type='button'
									key={`addNew_${type}`}
									className='-ml-px relative items-center flex-1 px-6 py-2 last:rounded-r-md border border-gray-300 bg-white text-sm font-medium text-gray-700 hover:bg-gray-50 focus:z-10 focus:outline-none focus:border-blue-500'
									onClick={() => {
										form.insertListItem('hyperparameters', {
											name: '',
											default: (type === 'bool') ? false : -1,
											...((type === 'paramgroup') && { params: {} }),
											...((type === 'stringlist') && { values: [''] }),
											...((type === 'integer' || type === 'float') && {
												min: '',
												max: '',
												step: '',
											}),
											type: type,
											useDefault: false,
										})
									}}
								>
									{type}
								</button>
							))}
						</span>
					</div>
				</InputSection>

				<div className={'flex-0 p-4 h-full grow-0'}>
					<div
						className="h-full grow-0 max-h-fit mb-4 overflow-y-scroll p-4 border-2 border-gray-300 border-dashed rounded-lg hover:border-gray-400"
						style={{ maxHeight: '60vh' }}
					>
						<div className="flex flex-col">
							{props.children}
						</div>
					</div>
				</div>
			</Fragment>
			<div className="text-right p-4">
				{(() => {
					if (permutations > 100000) {
						return (
							<span className='text-xl font-bold text-red-600'>
								WARNING: This is NOT Recommended. Expected Permutations: {text}
							</span>
						);
					} else if (permutations > 10000) {
						return (
							<span className='text-md font-bold text-orange-600'>
								Caution: This will take time. Expected Permutations: {text}
							</span>
						);
					} else if (permutations > 1000) {
						return (
							<span className='text-sm font-medium text-yellow-600'>
								Expected Permutations: {text}
							</span>
						);
					} else {
						return (
							<span className='text-sm font-bold'>
								Expected Permutations: {text}
							</span>
						);
					}
				})()}
			</div>
		</div>
	);
};


