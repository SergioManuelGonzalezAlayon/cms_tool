from enum import Enum
"""
This is the Current Measurement Tool (CMS) Bench deamon that will start once the
SBC, hopefully a RPi, enters user space. This will notify the HWMux that a CMS
Bench with an specific board is available for usage as a resource.

However, the CMS Bench shall have different ways of testing the apps:

* A full_sweep is intended to test an SiSDK before release to gather data for the
CMS database to be used by the Battery Estimator
* A default test is going to be used on a PR that needs to test the current commit and
the previous testing commit to conclude weather this commit enhances to violates the
CMS QA policy.


The CMS Bench is capable of not just accepting FW to be tested but to retreive images
from artifactory.
"""

class CMSBenchState(Enum):
    IDLE = 0
    REMOTE_TESTING = 1
    REMOTE_DEBUG = 2
    SISDK_TESTING = 3

class CMSBenchResultAnalysis(Enum):
    DEBUG = -1
    DEFAULT = 0
    FULL_SWEEP = 1
    DIFF = 2

class CMSBench:
    def __init__(self):
        self.state : CMSBenchState = CMSBenchState.IDLE
        self.test_queue = []
        self.result_db = ""
        self.dut_serial = ""
        self.tester_serial = ""
        self.js_interface = ""
    
    def init(self, dut, tester, js):
        """
        Start the CMSBench operation by checking the resource availability to run the tests 
        """
        raise NotImplementedError

    def start_service(self):
        """
        Start the CMS Bench Server to listen for any CMS Client requests.
        """
        raise NotImplementedError

    def HWMux_register(self):
        """
        Register CMS Bench to the HWMux so that CMS Clients can discover it.
        """
        raise NotImplementedError

    def client_check_permission(self):
        """
        Check client permission to run the type of test requested
        """
        raise NotImplementedError

    def client_req_handle(self):
        """
        Handle the following CMS Client request
        1.- Bench status
        2.- SiSDK FW version
        3.- Test progress
        4.- DUT FW Image Receive
        5.- Tester FW Image Receive
        6.- Run test
        7.- Debug mode
        """
        raise NotImplementedError
    
    def client_reg_deny(self):
        """
        Deny a CMS Client request
        """
        raise NotImplementedError

    def smoke_apps(self, dut_app, tester_app):
        """
        Run the smoke tests to check the validity of the FW apps retreived, this avoids running a whole
        suite for applications that either do not have the Wake-Lock component 
        """
        raise NotImplementedError

    def run_test(self,
                 dut_app=None,
                 tester_app=None,
                 app_location=True,
                 analysis: CMSBenchResultAnalysis=CMSBenchResultAnalysis.DEFAULT):
        """
        Run a test suite for the DUT and Tester applications as requested by the CMS Client
        """
        raise NotImplementedError

    def send_bench_info(self):
        """
        Send CMS Bench information to the CMS Client
        """
        raise NotImplementedError
    
    def send_test_results(self):
        """
        Send the test artifact for the last completed test before continuing the testing,
        this way it CMS Client can keep track of the completed tests and their respective
        failures during the whole testing stage.
        """
        raise NotImplementedError

    def set_status(self):
        """
        Set the CMS Bench status based on CMS Client request or system 
        status
        """
        raise NotImplementedError

    def set_mode(self):
        """
        Set the CMS Bench in different test modes before proceeding
        with the CMS Client requests.
        """
        raise NotImplementedError
