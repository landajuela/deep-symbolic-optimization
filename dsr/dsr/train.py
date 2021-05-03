"""Defines main training loop for deep symbolic regression."""

import os
import multiprocessing
import time
from itertools import compress
from collections import defaultdict

import tensorflow as tf
import numpy as np

from dsr.program import Program, from_tokens
from dsr.utils import empirical_entropy, get_duration, weighted_quantile
from dsr.memory import Batch, make_queue
from dsr.variance import quantile_variance
from dsr.train_stats import StatsLogger

# Ignore TensorFlow warnings
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)

# Set TensorFlow seed
tf.random.set_random_seed(0)

# Work for multiprocessing pool: optimize constants and compute reward
def work(p):
    optimized_constants = p.optimize()
    return optimized_constants, p.base_r





# def sympy_work(p):
#     sympy_expr = p.sympy_expr
#     str_sympy_expr = repr(p.sympy_expr) if sympy_expr != "N/A" else repr(p)
#     return sympy_expr, str_sympy_expr

def learn(sess, controller, pool, gp_controller,
          logdir="./log", n_epochs=None, n_samples=1e6,
          batch_size=1000, complexity="length", complexity_weight=0.001,
          const_optimizer="minimize", const_params=None, alpha=0.1,
          epsilon=0.01, n_cores_batch=1, verbose=True, save_summary=True,
          output_file=None, save_all_epoch=False, baseline="ewma_R",
          b_jumpstart=True, early_stopping=False, hof=10, eval_all=False,
          save_pareto_front=False, debug=0, use_memory=False, memory_capacity=1e4,
          warm_start=None, memory_threshold=None, save_positional_entropy=False,
          n_objects=1, save_cache=False, save_cache_r_min=0.9, save_buffered=None):
          # TODO: Let tasks set n_objects, i.e. LunarLander-v2 would set n_objects = 2. For now, allow the user to set it by passing it in here.


    """
    Executes the main training loop.

    Parameters
    ----------
    sess : tf.Session
        TenorFlow Session object.

    controller : dsr.controller.Controller
        Controller object used to generate Programs.

    pool : multiprocessing.Pool or None
        Pool to parallelize reward computation. For the control task, each
        worker should have its own TensorFlow model. If None, a Pool will be
        generated if n_cores_batch > 1.

    logdir : str, optional
        Name of log directory.

    n_epochs : int or None, optional
        Number of epochs to train when n_samples is None.

    n_samples : int or None, optional
        Total number of expressions to sample when n_epochs is None. In this
        case, n_epochs = int(n_samples / batch_size).

    batch_size : int, optional
        Number of sampled expressions per epoch.

    complexity : str, optional
        Complexity penalty name.

    complexity_weight : float, optional
        Coefficient for complexity penalty.

    const_optimizer : str or None, optional
        Name of constant optimizer.

    const_params : dict, optional
        Dict of constant optimizer kwargs.

    alpha : float, optional
        Coefficient of exponentially-weighted moving average of baseline.

    epsilon : float or None, optional
        Fraction of top expressions used for training. None (or
        equivalently, 1.0) turns off risk-seeking.

    n_cores_batch : int, optional
        Number of cores to spread out over the batch for constant optimization
        and evaluating reward. If -1, uses multiprocessing.cpu_count().

    verbose : bool, optional
        Whether to print progress.

    save_summary : bool, optional
        Whether to write TensorFlow summaries.

    output_file : str, optional
        Filename to write results for each iteration.

    save_all_r : bool, optional
        Whether to save all rewards for each iteration.

    baseline : str, optional
        Type of baseline to use: grad J = (R - b) * grad-log-prob(expression).
        Choices:
        (1) "ewma_R" : b = EWMA(<R>)
        (2) "R_e" : b = R_e
        (3) "ewma_R_e" : b = EWMA(R_e)
        (4) "combined" : b = R_e + EWMA(<R> - R_e)
        In the above, <R> is the sample average _after_ epsilon sub-sampling and
        R_e is the (1-epsilon)-quantile estimate.

    b_jumpstart : bool, optional
        Whether EWMA part of the baseline starts at the average of the first
        iteration. If False, the EWMA starts at 0.0.

    early_stopping : bool, optional
        Whether to stop early if stopping criteria is reached.

    hof : int or None, optional
        If not None, number of top Programs to evaluate after training.

    eval_all : bool, optional
        If True, evaluate all Programs. While expensive, this is useful for
        noisy data when you can't be certain of success solely based on reward.
        If False, only the top Program is evaluated each iteration.

    save_pareto_front : bool, optional
        If True, compute and save the Pareto front at the end of training.

    debug : int, optional
        Debug level, also passed to Controller. 0: No debug. 1: Print initial
        parameter means. 2: Print parameter means each step.

    use_memory : bool, optional
        If True, use memory queue for reward quantile estimation.

    memory_capacity : int
        Capacity of memory queue.

    warm_start : int or None
        Number of samples to warm start the memory queue. If None, uses
        batch_size.

    memory_threshold : float or None
        If not None, run quantile variance/bias estimate experiments after
        memory weight exceeds memory_threshold.

    save_positional_entropy : bool, optional
        Whether to save evolution of positional entropy for each iteration.

    save_cache : bool
        Whether to save the str, count, and r of each Program in the cache.

    save_cache_r_min : float or None
        If not None, only keep Programs with r >= r_min when saving cache.

    save_buffered : int or None
            If None, statistics per epoch are saved immediately after computed.
            If an int number, the statistics will be kept in a buffer (in memory) and will be only saved in disk with
            frequency defined by save_buffered (a zero or negative number means that the statistics will be kept in the
            buffer until the training ends)

    Returns
    -------
    result : dict
        A dict describing the best-fit expression (determined by base_r).
    """
    all_r_size              = batch_size

    if gp_controller is not None:
        run_gp_meld             = True
        gp_verbose              = gp_controller.config_gp_meld["verbose"]
        if gp_controller.config_gp_meld["train_n"]:
            all_r_size              = batch_size+gp_controller.config_gp_meld["train_n"]
        else:
            all_r_size              = batch_size+1
    else:
        gp_controller           = None
        run_gp_meld             = False
        gp_verbose              = False

    # Config assertions and warnings
    assert n_samples is None or n_epochs is None, "At least one of 'n_samples' or 'n_epochs' must be None."

    # TBD: REFACTOR
    # Set the complexity functions
    Program.set_complexity_penalty(complexity, complexity_weight)

    # TBD: REFACTOR
    # Set the constant optimizer
    const_params = const_params if const_params is not None else {}
    Program.set_const_optimizer(const_optimizer, **const_params)

    # Initialize compute graph
    sess.run(tf.global_variables_initializer())

    if debug:
        tvars = tf.trainable_variables()
        def print_var_means():
            tvars_vals = sess.run(tvars)
            for var, val in zip(tvars, tvars_vals):
                print(var.name, "mean:", val.mean(),"var:", val.var())

    # Create the pool of workers, if pool is not already given
    if pool is None:
        if n_cores_batch == -1:
            n_cores_batch = multiprocessing.cpu_count()
        if n_cores_batch > 1:
            pool = multiprocessing.Pool(n_cores_batch)

    # Create the priority queue
    k = controller.pqt_k
    if controller.pqt and k is not None and k > 0:
        priority_queue = make_queue(priority=True, capacity=k)
    else:
        priority_queue = None

    # Create the memory queue
    if use_memory:
        assert epsilon is not None and epsilon < 1.0, \
            "Memory queue is only used with risk-seeking."
        memory_queue = make_queue(controller=controller, priority=False,
                                  capacity=int(memory_capacity))

        # Warm start the queue
        # TBD: Parallelize. Abstract sampling a Batch
        warm_start = warm_start if warm_start is not None else batch_size
        actions, obs, priors = controller.sample(warm_start)
        programs = [from_tokens(a, optimize=True, n_objects=n_objects) for a in actions]
        r = np.array([p.r for p in programs])
        l = np.array([len(p.traversal) for p in programs])
        on_policy = np.array([p.on_policy for p in programs])
        sampled_batch = Batch(actions=actions, obs=obs, priors=priors,
                              lengths=l, rewards=r, on_policy=on_policy)
        memory_queue.push_batch(sampled_batch, programs)
    else:
        memory_queue = None

    if debug >= 1:
        print("\nInitial parameter means:")
        print_var_means()

    # For stochastic Tasks, store each base_r computation for each unique traversal
    if Program.task.stochastic:
        base_r_history = {} # Dict from Program str to list of base_r values
        # It's not really clear whether Programs with const should enter the hof for stochastic Tasks
        assert Program.library.const_token is None, \
            "Constant tokens not yet supported with stochastic Tasks."
        assert not save_pareto_front, "Pareto front not supported with stochastic Tasks."
    else:
        base_r_history = None

    # Main training loop
    p_final = None
    base_r_best = -np.inf
    r_best = -np.inf
    prev_r_best = None
    prev_base_r_best = None
    ewma = None if b_jumpstart else 0.0 # EWMA portion of baseline
    n_epochs = n_epochs if n_epochs is not None else int(n_samples / batch_size)
    all_r = np.zeros(shape=(all_r_size), dtype=np.float32)

    positional_entropy = np.zeros(shape=(n_epochs, controller.max_length), dtype=np.float32)

    logger = StatsLogger(sess,  logdir, save_summary, output_file, save_all_epoch, hof, save_pareto_front,
                         save_positional_entropy, save_cache, save_cache_r_min, save_buffered)
    nevals              = 0
    #program_val_log     = []

    start_time = time.time()
    print("\n-- START TRAINING -------------------")
    for epoch in range(n_epochs):

        if gp_verbose:
            print("************************************************************************")
            print("EPOCH {}".format(epoch))
            print("************************")

        # Set of str representations for all Programs ever seen
        s_history = set(base_r_history.keys() if Program.task.stochastic else Program.cache.keys())

        # Sample batch of expressions from controller
        # Shape of actions: (batch_size, max_length)
        # Shape of obs: [(batch_size, max_length)] * 3
        # Shape of priors: (batch_size, max_length, n_choices)
        actions, obs, priors = controller.sample(batch_size)

        nevals += batch_size

        if run_gp_meld:
            '''
                Given the set of 'actions' we have so far, we will use them as a prior seed into
                the GP controller. It will take care of conversion to its own population data
                structures. It will return programs, observations, actions that are compat with
                the current way we do things in train.py.
            '''
            deap_programs, deap_obs, deap_actions, deap_priors = gp_controller(actions)
            nevals += gp_controller.nevals

            if gp_verbose:
                print("************************")
                print("Number of Evaluations: {}".format(nevals))
                print("************************")
                print("Deap Programs:")
                deap_programs[0].print_stats()
                print("************************")

        # Instantiate, optimize, and evaluate expressions
        if pool is None:
            programs = [from_tokens(a, optimize=True, n_objects=n_objects) for a in actions]
        else:
            # To prevent interfering with the cache, un-optimized programs are
            # first generated serially. Programs that need optimizing are
            # optimized optimized in parallel. Since multiprocessing operates on
            # copies of programs, we manually set the optimized constants and
            # base reward after the pool joins.
            programs = [from_tokens(a, optimize=False, n_objects=n_objects) for a in actions]

            # Filter programs that have not yet computed base_r
            # TBD: Refactor with needs_optimizing flag or similar?
            programs_to_optimize = list(set([p for p in programs if "base_r" not in p.__dict__]))

            # Optimize and compute base_r
            results = pool.map(work, programs_to_optimize)
            for (optimized_constants, base_r), p in zip(results, programs_to_optimize):
                p.set_constants(optimized_constants)
                p.base_r = base_r

        # If we run GP, insert GP Program, actions, priors (blank) and obs.
        # We may option later to return these to the controller.
        if run_gp_meld:
            programs    = programs + deap_programs
            actions     = np.append(actions, deap_actions, axis=0)
            obs         = [np.append(obs[0], deap_obs[0], axis=0),
                           np.append(obs[1], deap_obs[1], axis=0),
                           np.append(obs[2], deap_obs[2], axis=0)]
            priors      = np.append(priors, deap_priors, axis=0)

        # Retrieve metrics
        '''
            base_r:   is the reward regardless of complexity penalty.
            r:        is reward with complexity subtracted. Note, if complexity_weight is 0 in the config, base_r = r
        '''
        base_r      = np.array([p.base_r for p in programs])
        r           = np.array([p.r for p in programs])
        r_train     = r

        # Need for Vanilla Policy Gradient (epsilon = null)
        p_train     = programs

        l           = np.array([len(p.traversal) for p in programs])
        s           = [p.str for p in programs] # Str representations of Programs
        on_policy   = np.array([p.on_policy for p in programs])
        invalid     = np.array([p.invalid for p in programs], dtype=bool)
        #all_r[epoch] = base_r

        if save_positional_entropy:
            positional_entropy[epoch] = np.apply_along_axis(empirical_entropy, 0, actions)

        if eval_all:
            success = [p.evaluate.get("success") for p in programs]
            # Check for success before risk-seeking, but don't break until after
            if any(success):
                p_final = programs[success.index(True)]

        # Update reward history
        if base_r_history is not None:
            for p in programs:
                key = p.str
                if key in base_r_history:
                    base_r_history[key].append(p.base_r)
                else:
                    base_r_history[key] = [p.base_r]

        # Store in variables the values for the whole batch (those variables will be modified below)
        base_r_max = np.max(base_r)
        base_r_best = max(base_r_max, base_r_best)
        base_r_full = base_r
        r_full = r
        l_full = l
        s_full = s
        actions_full = actions
        invalid_full = invalid
        r_max = np.max(r)
        r_best = max(r_max, r_best)
        traversals_full = [str(p.traversal) for p in programs]


        '''
            Risk-seeking policy gradient: only train on top epsilon fraction of sampled expressions
            Note: controller.train_step(r_train, b_train, actions, obs, priors, mask, priority_queue)

            GP Integration note:

            For the moment, GP samples get added on top of the epsilon samples making it slightly larger. 
            This will be changed in the future when we integrate off policy support.
        '''
        if epsilon is not None and epsilon < 1.0:
            # Compute reward quantile estimate
            if use_memory: # Memory-augmented quantile

                # Get subset of Programs not in buffer
                unique_programs = [p for p in programs \
                                   if p.str not in memory_queue.unique_items]
                N = len(unique_programs)

                # Get rewards
                memory_r = memory_queue.get_rewards()
                sample_r = [p.r for p in unique_programs]
                combined_r = np.concatenate([memory_r, sample_r])

                # Compute quantile weights
                memory_w = memory_queue.compute_probs()
                if N == 0:
                    print("WARNING: Found no unique samples in batch!")
                    combined_w = memory_w / memory_w.sum() # Renormalize
                else:
                    sample_w = np.repeat((1 - memory_w.sum()) / N, N)
                    combined_w = np.concatenate([memory_w, sample_w])

                # Quantile variance/bias estimates
                if memory_threshold is not None:
                    print("Memory weight:", memory_w.sum())
                    if memory_w.sum() > memory_threshold:
                        quantile_variance(memory_queue, controller, batch_size, epsilon, epoch)

                # Compute the weighted quantile
                quantile = weighted_quantile(values=combined_r, weights=combined_w, q=1 - epsilon)

            else: # Empirical quantile
                quantile = np.quantile(r, 1 - epsilon, interpolation="higher")

            # These guys can contain the GP solutions if we run GP
            '''
                Here we get the returned as well as stored programs and properties.

                If we are returning the GP programs to the controller, p and r will be exactly the same
                as p_train and r_train. Othewrwise, p and r will still contain the GP programs so they
                can still fall into the hall of fame. p_train and r_train will be different and no longer
                contain the GP program items.
            '''

            keep        = base_r >= quantile
            base_r      = base_r[keep]
            l           = l[keep]
            s           = list(compress(s, keep))
            invalid     = invalid[keep]

            # Option: don't keep the GP programs for return to controller
            if run_gp_meld and not gp_controller.return_gp_obs:
                if gp_verbose:
                    print("GP solutions NOT returned to controller")
                '''
                    If we are not returning the GP components to the controller, we will remove them from
                    r_train and p_train by augmenting 'keep'. We just chop off the GP elements which are indexed
                    from batch_size to the end of the list.
                '''
                _r                  = r[keep]
                _p                  = list(compress(programs, keep))
                keep[batch_size:]   = False
                r_train             = r[keep]
                p_train             = list(compress(programs, keep))

                '''
                    These contain all the programs and rewards regardless of whether they are returned to the controller.
                    This way, they can still be stored in the hall of fame.
                '''
                r                   = _r
                programs            = _p
            else:
                if run_gp_meld and gp_verbose:
                    print("{} GP solutions returned to controller".format(gp_controller.config_gp_meld["train_n"]))
                '''
                    Since we are returning the GP programs to the contorller, p and r are the same as p_train and r_train.
                '''
                r_train = r         = r[keep]
                p_train = programs  = list(compress(programs, keep))

            '''
                get the action, observation, priors and on_policy status of all programs returned to the controller.
            '''
            actions     = actions[keep, :]
            obs         = [o[keep, :] for o in obs]
            priors      = priors[keep, :, :]
            on_policy   = on_policy[keep]

        # Clip bounds of rewards to prevent NaNs in gradient descent
        r       = np.clip(r,        -1e6, 1e6)
        r_train = np.clip(r_train,  -1e6, 1e6)

        # Compute baseline
        # NOTE: pg_loss = tf.reduce_mean((self.r - self.baseline) * neglogp, name="pg_loss")
        if baseline == "ewma_R":
            ewma = np.mean(r_train) if ewma is None else alpha*np.mean(r_train) + (1 - alpha)*ewma
            b_train = ewma
        elif baseline == "R_e": # Default
            ewma = -1
            b_train = quantile
        elif baseline == "ewma_R_e":
            ewma = np.min(r_train) if ewma is None else alpha*quantile + (1 - alpha)*ewma
            b_train = ewma
        elif baseline == "combined":
            ewma = np.mean(r_train) - quantile if ewma is None else alpha*(np.mean(r_train) - quantile) + (1 - alpha)*ewma
            b_train = quantile + ewma

        # Compute sequence lengths
        lengths = np.array([min(len(p.traversal), controller.max_length)
                            for p in p_train], dtype=np.int32)

        # Create the Batch
        sampled_batch = Batch(actions=actions, obs=obs, priors=priors,
                              lengths=lengths, rewards=r_train, on_policy=on_policy)

        # Update and sample from the priority queue
        if priority_queue is not None:
            priority_queue.push_best(sampled_batch, programs)
            pqt_batch = priority_queue.sample_batch(controller.pqt_batch_size)
        else:
            pqt_batch = None

        # Train the controller
        summaries = controller.train_step(b_train, sampled_batch, pqt_batch)

        # Collect sub-batch statistics and write output
        logger.save_stats(base_r_full, r_full, l_full, actions_full, s_full, invalid_full, traversals_full,  base_r, r,
                          l, actions, s, invalid,  base_r_best, base_r_max, r_best, r_max, ewma, summaries, epoch,
                          s_history)

        # Update the memory queue
        if memory_queue is not None:
            memory_queue.push_batch(sampled_batch, programs)

        # Update new best expression
        new_r_best = False
        new_base_r_best = False

        if prev_r_best is None or r_max > prev_r_best:
            new_r_best = True
            p_r_best = programs[np.argmax(r)]

        if prev_base_r_best is None or base_r_max > prev_base_r_best:
            new_base_r_best = True
            p_base_r_best = programs[np.argmax(base_r)]

        prev_r_best = r_best
        prev_base_r_best = base_r_best

        if gp_verbose:
            print("************************")
            print("Best epoch Program:")
            programs[np.argmax(base_r)].print_stats()
            print("************************")
            print("All time best Program:")
            p_base_r_best.print_stats()
            print("************************")

        # Print new best expression
        if verbose:
            if new_r_best or new_base_r_best:
                print("[{}] Training epoch {}/{}, current best R: {:.4f}".format(get_duration(start_time), epoch, n_epochs, prev_r_best))
            if new_r_best and new_base_r_best:
                if p_r_best == p_base_r_best:
                    print("\n\t** New best overall")
                    p_r_best.print_stats()
                else:
                    print("\n\t** New best reward")
                    p_r_best.print_stats()
                    print("...and new best base reward")
                    p_base_r_best.print_stats()

            elif new_r_best:
                print("\n\t** New best reward")
                p_r_best.print_stats()

            elif new_base_r_best:
                print("\n\t** New best base reward")
                p_base_r_best.print_stats()

        # Stop if early stopping criteria is met
        if eval_all and any(success):
            #all_r = all_r[:(epoch + 1)]
            print("[{}] Early stopping criteria met; breaking early.".format(get_duration(start_time)))
            break
        if early_stopping and p_base_r_best.evaluate.get("success"):
            #all_r = all_r[:(epoch + 1)]
            print("[{}] Early stopping criteria met; breaking early.".format(get_duration(start_time)))
            break

        if verbose and epoch > 0 and epoch % 10 == 0:
            print("[{}] Training epoch {}/{}, current best R: {:.4f}".format(get_duration(start_time), epoch, n_epochs, prev_r_best))

        if debug >= 2:
            print("\nParameter means after epoch {} of {}:".format(epoch+1, n_epochs))
            print_var_means()



        if run_gp_meld:
            if nevals > n_samples:
                print("************************")
                print("All time best Program:")
                p_base_r_best.print_stats()
                print("************************")
                print("Max Number of Samples Exceeded. Exiting...")
                break
    print("-------------------------------------")

    print("\n-- PROCESSING RESULTS ---------------")

    if verbose:
        print("Evaluating the hall of fame...")
    #Save all results available only after all epochs are finished.
    logger.save_results(positional_entropy, base_r_history, pool)

    # Print error statistics of the cache
    n_invalid = 0
    error_types = defaultdict(lambda : 0)
    error_nodes = defaultdict(lambda : 0)
    for p in Program.cache.values():
        if p.invalid:
            n_invalid += p.count
            error_types[p.error_type] += p.count
            error_nodes[p.error_node] += p.count
    if n_invalid > 0:
        total_samples = (epoch + 1)*batch_size # May be less than n_samples if breaking early
        print("Invalid expressions: {} of {} ({:.1%}).".format(n_invalid, total_samples, n_invalid/total_samples))
        print("Error type counts:")
        for error_type, count in error_types.items():
            print("  {}: {} ({:.1%})".format(error_type, count, count/n_invalid))
        print("Error node counts:")
        for error_node, count in error_nodes.items():
            print("  {}: {} ({:.1%})".format(error_node, count, count/n_invalid))

    # Print the priority queue at the end of training
    if verbose and priority_queue is not None:
        for i, item in enumerate(priority_queue.iter_in_order()):
            print("\nPriority queue entry {}:".format(i))
            p = Program.cache[item[0]]
            p.print_stats()

    # Close the pool
    if pool is not None:
        pool.close()
    print("-------------------------------------")

    # Return statistics of best Program
    p = p_final if p_final is not None else p_base_r_best
    result = {
        "r" : p.r,
        "base_r" : p.base_r,
    }
    result.update(p.evaluate)
    result.update({
        "expression" : repr(p.sympy_expr),
        "traversal" : repr(p),
        "program" : p
        })

    return result
