import time
import sys
import threading
import httplib
import urllib
import urlparse
import socket
import datetime
import signal
import copy
import pickle
import re

# import numpy as np
from itertools import cycle
from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
from SocketServer import ThreadingMixIn

from still_shared import InputThread
from still_shared import handle_keyboard_input
from task_server import TaskClient


#  Setup the lib path ./lib/  as a spot to check for python libraries
# basedir = os.path.dirname(os.path.realpath(__file__))[:-3]
# sys.path.append(basedir + 'bin')

logger = True  # This is just here because the jedi syntax checker is dumb.

MAXFAIL = 5  # Jon : move this into config
TIME_INT_FOR_NEW_TM_CHECK = 90
TIME_INT_FOR_NEW_MC_CHECK = 300


def file2jd(zenuv):
    return re.findall(r'\d+\.\d+', zenuv)[0]


def file2pol(zenuv):
    return re.findall(r'\.(.{2})\.', zenuv)[0]

str2pol = {
    'I': 1,   # Stokes Paremeters
    'Q': 2,
    'U': 3,
    'V': 4,
    'rr': -1,   # Circular Polarizations
    'll': -2,
    'rl': -3,
    'lr': -4,
    'xx': -5,   # Linear Polarizations
    'yy': -6,
    'xy': -7,
    'yx': -8,
}

pol2str = {}
for k in str2pol:
    pol2str[str2pol[k]] = k


def action_cmp(x, y):
    return cmp(x.priority, y.priority)


class Action:
    '''An Action performs a task on an observation, and is scheduled by a Scheduler.'''

    def __init__(self, obs, task, neighbor_status,
                 task_client, workflow, still, timeout=3600.):
        '''f:obs, task:target status,
        neighbor_status:status of adjacent obs (do not enter a status for a non-existent neighbor
        still:still action will run on.'''
        self.obs = obs
        self.task = task
        self.is_transfer = False
        self.neighbor_status = neighbor_status
        self.still = still
        self.priority = 0
        self.launch_time = -1
        self.timeout = timeout
        self.wf = workflow
        self.task_client = task_client

    def set_priority(self, p):
        '''Assign a priority to this action.  Highest priorities are scheduled first.'''
        self.priority = p

    def has_prerequisites(self):
        '''For the given task, check that neighbors are in prerequisite state.
        We don't check that the center obs is in the prerequisite state,
        s this action could not have been generated otherwise.'''

        try:
            index1 = self.wf.workflow_actions.index(
                self.wf.action_prereqs[self.task][0])
        except:
            return True
        try:
            index2 = self.wf.workflow_actions.index(
                self.wf.action_prereqs[self.task][1])
        except:
            index2 = None

        for status_of_neighbor in self.neighbor_status:
            if status_of_neighbor in (None, '', 'NEW'):
                # obs not entered in DB or not ready for processing
                return False

            index_of_neighbor_status = self.wf.workflow_actions.index(
                status_of_neighbor)

            if index1 is not None and index_of_neighbor_status < index1:
                return False

            if index2 is not None and index_of_neighbor_status >= index2:
                return False

        return True

    def launch(self, launch_time=None):
        '''Run this task.'''
        if launch_time is None:
            launch_time = time.time()
        self.launch_time = launch_time
        logger.debug('Action: launching (%s,%s) on still %s' %
                     (self.task, self.obs, self.task_client.host_port[0]))
        return self.run_remote_task(action_type="NEW_TASK")

    def timed_out(self, curtime=None):
        assert(self.launch_time > 0)  # Error out if action was not launched
        if curtime is None:
            curtime = time.time()
        return curtime > self.launch_time + self.timeout

    def run_remote_task(self, task="", action_type=""):
        if task == "":
            task = self.task

        logger.debug('Action: task_client(%s,%s)' % (task, self.obs))
        connection_status, error_count = self.task_client.transmit(
            task, self.obs, action_type)

        return connection_status


class MonitorHandler(BaseHTTPRequestHandler):
    ###
    # Handles all incoming requests for the http interface to the monitoring port
    ###

    def get_from_server(self, still, data_type):

        if data_type == "INFO_TASKS":
            conn_type = "GET"
            conn_path = "/INFO_TASKS"
            conn_params = ""
            response_data = ""
        try:  # Attempt to open a socket to a server and send over task instructions
            tm_info = self.server.dbi.get_still_info(still)
            logger.debug("connecting to TaskServer %s" % tm_info.ip_addr)

            conn = httplib.HTTPConnection(
                tm_info.ip_addr, tm_info.port, timeout=20)
            conn.request(conn_type, conn_path)
            response = conn.getresponse()
            response_status = response.status
            response_reason = response.reason
            response_data = response.read()
        except:
            logger.exception("Could not connect to server %s on port : %s" % (
                tm_info.ip_addr, tm_info.port))
        finally:
            conn.close()
        return response_data

    def do_GET(self):
        parsed_path = urlparse.urlparse(self.path)
        self.send_response(200)
        self.end_headers()

        if parsed_path.path == "/":
            message = ""
            open_obs_count = len(self.server.dbi.list_open_observations())
            failed_obs_count = len(
                self.server.dbi.list_observations_with_cur_stage('FAILED'))
            killed_obs_count = len(
                self.server.dbi.list_observations_with_cur_stage('KILLED'))
            completed_obs_count = len(
                self.server.dbi.list_observations_with_status('COMPLETE'))
            message += "Open Obs count: %s  -  Completed Obs count : %s  -  Failed Obs count: %s  -  Killed Obs count: %s\n\n" % \
                       (open_obs_count, completed_obs_count,
                        failed_obs_count, killed_obs_count)
            for still in self.server.launched_actions:
                tm_info = self.server.dbi.get_still_info(still)
                message += "Host: %s - CPU Count: %s  - Load: %s%%  - Memory(used/tot): %s/%s GB - Task#(cur/max): %s/%s \n\n" % \
                           (still, tm_info.number_of_cores, tm_info.current_load, (tm_info.total_memory - tm_info.free_memory),
                            tm_info.total_memory, tm_info.cur_num_of_tasks, tm_info.max_num_of_tasks)
                pickled_data_on_tasks = self.get_from_server(
                    still, "INFO_TASKS")
                mydict = pickle.loads(pickled_data_on_tasks)

                for mytask in mydict:
                    dt = datetime.timedelta(
                        seconds=(int(time.time() - mytask['start_time'])))
                    dt_list = dt.days, dt.seconds // 3600, (dt.seconds //
                                                            60) % 60
                    message += "  * Obsnum: %s  -  Task: %s  -  Proc Status: %s  -  CPU: %s%%  -  Mem: %sMB  -  Runtime: %sd %sh %sm \n" % \
                               (mytask['obsnum'], mytask['task'], mytask['proc_status'],
                                mytask['cpu_percent'], mytask['mem_used'] / (1024 ** 2), dt_list[0], dt_list[1], dt_list[2])
                message += '\n'
            self.wfile.write(message)
            self.wfile.write('\n')
        return


class Scheduler(ThreadingMixIn, HTTPServer):
    ###
    # A Scheduler reads a DataBaseInterface to determine what Actions can be
    # taken, and then schedules them on stills according to priority.'''
    ###

    def __init__(self, task_clients, workflow, sg):
        global logger
        logger = sg.logger
        self.myhostname = socket.gethostname()

        # Class us into HTTPServer so we can make calls from TaskHandler into
        # this class via self.server.
        HTTPServer.__init__(self, (self.myhostname, 8080), MonitorHandler)
        # Might as well have it around in case I find I need something from it...
        # Its just a little memory
        self.sg = sg
        self.nstills = len(sg.hosts)  # preauto
        self.actions_per_still = sg.actions_per_still
        # Jon : This isn't used...
        self.transfers_per_still = sg.transfers_per_still
        self.block_size = sg.block_size  # preauto
        self.timeout = sg.timeout
        self.sleep_time = sg.sleep_time

        self.lock_all_neighbors_to_same_still = workflow.lock_all_neighbors_to_same_still
        self.active_obs = []
        self.active_obs_dict = {}
        self.action_queue = []
        self.dbi = sg.dbi
        self.launched_actions = {}

        self.keep_running = False
        self.failcount = {}
        # Jon: Moved the workflow class to instantiated on object creation,
        # should do the same for dbi probably
        self.wf = workflow
        self.task_clients = {}
        self.stills = []

        # Enabled clean shutdown after Cntrl-C event.
        signal.signal(signal.SIGINT, self.signal_handler)

        logger.info("Starting monitoring interface.")
        # Launch a thread of a multithreaded http server to view information on
        # currently running tasks
        threading.Thread(target=self.serve_forever).start()

        # If task_clients is set to AUTO then check the db for still servers
        if task_clients[0].host_port[0] == "AUTO":
            self.find_all_taskmanagers()
            self.auto = 1
        else:
            self.auto = 0
            self.task_clients = task_clients

    def signal_handler(self, signum, frame):
        logger.info("Caught Ctrl-C, Initiating clean shutdown.")
        self.shutdown()

    def find_all_taskmanagers(self):
        ###
        # find_all_taskmanagers : Check the database for all available stills with status OK
        #  Should also remove stills that have gone offline.
        ###
        logger.debug("looking for TaskManagers...")
        self.stills = self.dbi.get_available_stills()
        while len(self.stills) < 1:
            logger.debug(
                "Can't find any TaskManagers! Waiting for 10sec and trying again")
            time.sleep(10)
            self.stills = self.dbi.get_available_stills()

        for still in self.stills:
            if still.hostname not in self.task_clients:
                logger.debug("Discovery of new TaskManager : %s" %
                             still.hostname)
                self.task_clients[still.hostname] = TaskClient(
                    self.dbi, still.hostname, self.wf, still.port, self.sg)
                self.launched_actions[still.hostname] = []
        return

    def ext_command_hook(self):
        return

    def check_taskmanager(self, tm):
        tm_info = self.dbi.get_still_info(tm)
        since = datetime.datetime.now() - datetime.timedelta(minutes=3)
        # Status not OK or hasn't checked-in in over 3min.
        if tm_info.status != "OK" or tm_info.last_checkin < since:
            logger.info("Removing offline TaskManager : %s" % tm_info.hostname)
            self.launched_actions.pop(tm_info.hostname, None)
            self.task_clients.pop(tm_info.hostname, None)
            for obs in self.dbi.get_obs_assigned_to_still(tm_info.hostname):
                if obs.obsnum in self.active_obs_dict:
                    self.active_obs_dict.pop(obs.obsnum)
                    self.active_obs.remove(obs.obsnum)

            return False

        # Check to ensure we are not at max # of tasks for the taskmanager
        elif tm_info.cur_num_of_tasks >= tm_info.max_num_of_tasks:
            return False

        return True

    def start(self, dbi, ActionClass=None, action_args=()):
        ###
        #  Start scheduler, loop forever and handle checking for new obsid's, new/removed taskmanagers etc..
        #      This loop can be terminated by q + Enter, and paused by p + enter
        ###
        self.user_input = InputThread()
        self.user_input.start()
        # The scheduler is just starting, for the first run if we have new obs
        # we need this to assign to proper taskmanagers
        self.initial_startup = True
        self.tm_cycle = cycle(self.stills)
        self.keep_running = True
        logger.info('Starting Scheduler')
        self.dbi = dbi
        last_checked_for_stills = time.time()
        last_checked_for_mc = time.time()

        while self.keep_running:
            if (time.time() - last_checked_for_stills) > TIME_INT_FOR_NEW_TM_CHECK:
                self.find_all_taskmanagers()
                last_checked_for_stills = time.time()
                logger.debug("Number of TaskManagers : %s" %
                             len(self.task_clients))

            if self.wf.log_to_mc:
                import mc_utils
                now = time.time()
                dt_check = now - last_checked_for_mc
                if dt_check.total_seconds() > TIME_INT_FOR_NEW_MC_CHECK:
                    # get total number of running tasks
                    ntasks = 0
                    launched_actions_copy = copy.deepcopy(self.launched_actions)
                    for tm in launched_actions_copy:
                        ntasks += len(self.get_launched_actions(tm, tx=False))

                    # get time since check-in in minutes
                    dt_check_min = dt_check / 60

                    # get time since boot in hours
                    boot_time = psutil.boot_time()
                    dt_boot = now - boot_time
                    dt_boot_hr = dt_boot / 60 / 60

                    # log to M&C
                    status = "OK"
                    logger.debug("Logging to M&C : {0} status, {1:5.2f} min since last check; {2}"
                                 " tasks running; {4:10.3} hr since boot".format(status, dt_check_min,
                                                                                 str(ntasks), dt_boot_hr))
                    mc_utils.add_mc_rtp_status(status, dt_check_min, ntasks, dt_boot_hr)

            self.ext_command_hook()
            self.get_new_active_obs()

            self.update_action_queue(ActionClass, action_args)
            launched_actions_copy = copy.copy(self.launched_actions)
            # Launch actions that can be scheduled
            for tm in launched_actions_copy:
                tm_info = self.dbi.get_still_info(tm)
                # Check if the TaskManager is still available, if not it will
                # pop it out
                if self.check_taskmanager(tm) is False:
                    continue

                # I think this will work
                while len(self.get_launched_actions(tm, tx=False)) < tm_info.max_num_of_tasks:
                    # logger.debug("Number of launched actions : %s" % len(self.get_launched_actions(tm, tx=False)))
                    # FIXME: MIght still be having a small issue when a TM goes
                    # offline and back on
                    action_from_queue = self.pop_action_queue(tm, tx=False)

                    if action_from_queue is not False:
                        # If we had a connection error stop trying until TM
                        # checks back in
                        if self.launch_action(action_from_queue) != "OK":
                            break
                    else:
                        break

            self.clean_completed_actions(self.dbi)

            keyboard_input = self.user_input.get_user_input()
            if keyboard_input is not None and keyboard_input != '':
                handle_keyboard_input(self, keyboard_input)
            else:
                time.sleep(self.sleep_time)
            # We've run once now, all obs were assigned via roundrobin if they
            # were not previously
            self.initial_startup = False
        self.shutdown()

    def shutdown(self):
        logger.info("Shutting down...")
        self.keep_running = False
        HTTPServer.shutdown(self)
        sys.exit(0)

    def get_all_neighbors(self, obsnum):
        ###
        # get_all_neighbors: Go down (and up) the rabbit hole and find ALL the neighbors of a particular obsid
        ###
        neighbor_obs_nums = []
        # Go ahead and add the current obsid to the list
        neighbor_obs_nums.append(obsnum)

        low_obs, high_obs = self.dbi.get_neighbors(obsnum)
        while high_obs is not None:  # Traverse the list UP to find all neighbors above this one
            neighbor_obs_nums.append(high_obs)
            high_obs = self.dbi.get_neighbors(high_obs)[1]

        while low_obs is not None:  # Traverse the list DOWN to find all neighbors above this one
            neighbor_obs_nums.append(low_obs)
            low_obs = self.dbi.get_neighbors(low_obs)[0]
        return neighbor_obs_nums

    def get_all_pol_neighbors(self, obsnum):
        # get all polarization neighbors
        return self.dbi.get_pol_neighbors(obsnum)

    def pop_action_queue(self, still, tx=False):
        '''Return highest priority action for the given still.'''
        # Seems like we're going through all the actions to find the ones for the particular still..
        # Should think about optimizing at some point
        for i in xrange(len(self.action_queue)):
            a = self.action_queue[i]
            if a.still == still and a.is_transfer == tx:
                return self.action_queue.pop(i)
        return False

    def get_launched_actions(self, still, tx=False):
        return [a for a in self.launched_actions[still] if a.is_transfer == tx]

    def launch_action(self, a):
        '''Launch the specified Action and record its launch for tracking later.'''
        self.launched_actions[a.still].append(a)
        connection_status = a.launch()
        return connection_status

    def kill_action(self, a):
        logger.info('Scheduler.kill_action: called on (%s,%s)' %
                    (a.task, a.obs))
        connection_status = a.run_remote_task(action_type="KILL_TASK")
        return connection_status

    def clean_completed_actions(self, dbi):
        '''Check launched actions for completion, timeout or fail'''

        for still in self.launched_actions:
            updated_actions = []
            for action in self.launched_actions[still]:
                obs = dbi.get_obs(action.obs)
                status = obs.status
                pid = dbi.get_obs_pid(action.obs)

                try:
                    self.failcount[str(action.obs) + status]
                except(KeyError):
                    self.failcount[str(action.obs) + status] = 0

                if status == action.task:
                    logger.info('Task %s for obs %s on still %s completed successfully.' % (
                        action.task, action.obs, still))

                elif action.timed_out():
                    logger.info('Task %s for obs %s on still %s TIMED OUT.' % (
                        action.task, action.obs, still))
                    if self.kill_action(action) != "OK":
                        break
                    self.failcount[str(action.obs) + status] += 1
                    # XXX make db entry for documentation

                elif pid == -9:
                    self.failcount[str(action.obs) + status] += 1
                    logger.info('Task %s for obs %s on still %s HAS DIED. failcount=%d' % (
                        action.task, action.obs, still, self.failcount[str(action.obs) + status]))

                else:  # still active
                    updated_actions.append(action)

            self.launched_actions[still] = updated_actions

    def already_launched(self, action):
        '''Determine if this action has already been launched.  Enforces
        fact that only one valid action can be taken for a given obs
        at any one time.'''
        for a in self.launched_actions[action.still]:
            if a.obs == action.obs:
                return True
        return False

    def get_new_active_obs(self):
        '''Check for any new obs that may have appeared.  Actions for
        these obs may potentially take priority over ones currently
        active.'''

        observations = []
        observations += self.dbi.list_open_observations_on_tm()
        for tm_name in self.launched_actions:
            observations += self.dbi.list_open_observations_on_tm(
                tm_hostname=tm_name)

        for open_obs in observations:
            if open_obs not in self.active_obs_dict:
                self.active_obs_dict[open_obs] = len(self.active_obs)
                self.active_obs.append(open_obs)
        return

    def update_action_queue(self, ActionClass=None, action_args=()):
        '''Based on the current list of active obs (which you might want
        to update first), generate a prioritized list of actions that
        can be taken.'''
        actions = []
        for myobs in self.active_obs:
            myobs_info = self.dbi.get_obs(myobs)
            if myobs_info.status == "COMPLETE":
                # remove from queue and clean out failcount dictionary
                self.remove_obs_from_action_queue(myobs_info)
                self.clean_failcount_dict(myobs_info)
            elif (myobs_info.stillhost not in self.task_clients
                  and myobs_info.stillhost):
                # remove from queue
                self.remove_obs_from_action_queue(myobs_info)
            else:
                if (myobs_info.current_stage_in_progress == "FAILED"
                    or myobs_info.current_stage_in_progress == "KILLED"):
                    # Get the failcount, and retry action if we have only failed a few times.
                    # Some failures are due to MemoryErrors, and not due to underlying code problems
                    try:
                        failcount = self.failcount[str(myobs_info.obsnum) + myobs_info.status]
                        logger.debug("update_action_queue: Retrying obsid : {0}, task : {1}, Status: {2}, "
                                     "TM: {3}, Failcount: {4}".format(
                                         myobs_info.obsnum, myobs_info.current_stage_in_progress,
                                         myobs_info.status, myobs_info.stillhost, failcount))
                    except KeyError:
                        # we should not reach this branch, but just in case...
                        failcount = 0
                        logger.debug("update_action_queue: Retrying obsid : {0}, task : {1}, Status: {2}, "
                                     "TM: {3}, Failcount: {4}".format(
                                         myobs_info.obsnum, myobs_info.current_stage_in_progress,
                                         myobs_info.status, myobs_info.stillhost, failcount))
                    if failcount >= MAXFAIL:
                        # actually remove it
                        self.remove_obs_from_action_queue(myobs_info)
                        # set status to "KILLED" in db to prevent further attempts
                        self.dbi.update_obs_current_stage(myobs_info.obsnum, "KILLED")
                    else:
                        # reset database
                        status = self.dbi.get_obs_status(myobs_info.obsnum)
                        self.dbi.update_obs_current_stage(myobs_info.obsnum, status)
                myaction = self.get_action(
                    myobs, ActionClass=ActionClass, action_args=action_args)
                if (myaction is not None) and (
                        self.already_launched(myaction) is not True):
                    if self.wf.prioritize_obs == 1:
                        myaction.set_priority(
                            self.determine_priority(myaction))

                    actions.append(myaction)

        # place most important actions first
        actions.sort(action_cmp, reverse=True)
        self.action_queue = actions  # completely throw out previous action list

        return

    def remove_obs_from_action_queue(self, obsinfo):
        '''
        Remove observation from action_queue and print debug info.

        Arguments:
           obsinfo -- observation info from dbi.get_obs(obsid)
        Returns:
           None
        '''
        self.active_obs_dict.pop(obsinfo.obsnum)
        self.active_obs.remove(obsinfo.obsnum)
        logger.debug("update_action_queue: Removing obsid : {0}, task : {1}, Status: {2}, TM: {3}".format(
            obsinfo.obsnum, obsinfo.current_stage_in_progress, obsinfo.status, obsinfo.stillhost))

    def get_action(self, obsnum, ActionClass=None, action_args=()):
        '''Find the next actionable step for obs f (one for which all
        prerequisites have been met.  Return None if no action is available.
        This function is allowed to return actions that have already been
        launched.
        ActionClass: a subclass of Action, for customizing actions.
            None defaults to the standard Action'''
        obsinfo = self.dbi.get_obs(obsnum)
        status = obsinfo.status

        if status == 'COMPLETE':
            # logger.debug("COMPLETE for obsid : %s" % obsnum)  # You can see
            # how often completeds are checked by uncommenting this.. its a LOT
            return None  # obs is complete

        if status == '' or status == 'NEW':
            # Not yet ready for processing.
            return None

        # Check that the still assigned to the obs is currently in the list of active stills
        # !!!!FIXME!!!  - Maybe fixed?
        if obsinfo.stillhost is not None:
            if any(still for still in self.stills if still.hostname ==
                   obsinfo.stillhost):
                pass
            else:
                return None
        if self.wf.neighbors == 1:  # FIX ME, I don't want to call the same thing twice.. its ugly
            neighbors = self.dbi.get_neighbors(obsnum)

            if None in neighbors:
                cur_step_index = self.wf.workflow_actions_endfile.index(status)
                next_step = self.wf.workflow_actions_endfile[
                    cur_step_index + 1]

            else:  # this is a normal file
                cur_step_index = self.wf.workflow_actions.index(status)
                next_step = self.wf.workflow_actions[cur_step_index + 1]

            neighbor_status = [self.dbi.get_obs_status(
                n) for n in neighbors if n is not None]
        elif self.wf.pol_neighbors == 1:
            pol_neighbors = self.dbi.get_pol_neighbors(obsnum)

            if len(pol_neighbors) != 3:
                logger.debug(
                    "File {} was expecting 3 pol_neighbors, got {} instead. Skipping...".format(
                        obsnum, str(len(pol_neighbors))))
                self.dbi.set_obs_status(obsnum, "COMPLETE")
                return None

            # decide when it's time to transition to actions_endfile
            try:
                cur_step_index = self.wf.workflow_actions.index(status)
                next_step = self.wf.workflow_actions[cur_step_index + 1]
            except IndexError:
                cur_step_index = self.wf.workflow_actions_endfile.index(status)
                next_step = self.workflow_actions_endfile[cur_step_index + 1]

            neighbor_status = [self.dbi.get_obs_status(
                n) for n in pol_neighbors if n is not None]
        else:
            cur_step_index = self.wf.workflow_actions.index(status)
            next_step = self.wf.workflow_actions[cur_step_index + 1]
            neighbor_status = 0

        still = self.dbi.get_obs_still_host(obsnum)

        if not still:
            if self.initial_startup is True:
                still = self.tm_cycle.next().hostname  # Balance out all the nodes on startup
            else:
                # Get a still for a new obsid if one doesn't already exist.
                still = self.obs_to_still(obsnum)
                if still is False:
                    return None

            # Assign the still to the obsid
            self.dbi.set_obs_still_host(obsnum, still)

            if self.lock_all_neighbors_to_same_still == 1:
                if self.wf.neighbors == 1:
                    for neighbor in self.get_all_neighbors(obsnum):
                        self.dbi.set_obs_still_host(neighbor, still)
                elif self.wf.pol_neighbors == 1:
                    for neighbor in self.get_all_pol_neighbors(obsnum):
                        self.dbi.set_obs_still_host(neighbor, still)
        # If the obsnum is assigned to a server that doesn't exist at the moment
        #   we need to skip it, maybe reassign later
        if still != 0:
            if ActionClass is None:
                ActionClass = Action

            a = ActionClass(obsnum, next_step, neighbor_status, self.task_clients[
                            still], self.wf, still, timeout=self.timeout)
            if self.wf.neighbors == 1:
                if a.has_prerequisites() is True:
                    return a
            elif self.wf.pol_neighbors == 1:
                if a.has_prerequisites() is True:
                    return a
            else:
                return a
        # logger.debug('scheduler.get_action: (%s,%s) does not have prereqs' % (a.task, a.obs))
        return None

    def determine_priority(self, action):
        '''Assign a priority to an action based on its status and the time
        order of the obs to which this action is attached.'''
        jdcnt = (float(file2jd(action.obs)) - 2415020) / \
            0.005  # get the jd and convert to an integer (0.005 is the length of a typical PAPER obs)
        pol = str2pol[file2pol(action.obs)]
        # pol, jdcnt = int(action.obs) / 2 ** 32, int(action.obs) % 2 ** 32  #
        # XXX maybe not make this have to explicitly match dbi bits
        return jdcnt * 4 + pol  # prioritize first by time, then by pol
        # XXX might want to prioritize finishing a obs already started before
        # moving to the latest one (at least, up to a point) to avoid a

        # build up of partial obs.  But if you prioritize obs already
        # started too excessively, then the queue could eventually fill with
        # partially completed tasks that are failing for some reason

    def obs_to_still(self, obs):
        ##############
        #   Check if a obsid has a still already, if it does simply return it.
        #   If it does not then lets find the lowest loaded (cpu) one and assign it.
        #   If none are under 80% then lets just wait around, they're busy enough as is.
        ##############
        mystill = self.dbi.get_obs_still_host(obs)
        if mystill:
            if mystill in self.task_clients:
                return mystill
            else:
                # We couldn't find its still server as its not in task_clients for whatever reason,
                # so punt for now
                logger.debug(
                    "Obs attached to non-existant STILL OBS : %s, STILL %s" % (obs, mystill))
                return 0
        else:
            still = self.dbi.get_most_available_still()
            if still is not False:
                return still
            else:
                return False

    def clean_failcount_dict(self, obsinfo):
        '''
        Go through the failcount dictionary and clean out entries corresponding to the specified obsid.

        Arguments:
           obsinfo -- observation info from dbi.get_obs(obsid)
        Returns:
           None
        '''
        # we will match keys in the dict with a regex
        # keys in the failcount dictionary are of the form obsid + task name
        for key in self.failcount.keys():
            if re.match(str(obsinfo.obsnum), key):
                self.failcount.pop(key)
        return
