
try:
    from deap import gp
    from deap import base
    from deap import tools
    from deap import creator
    from deap import algorithms
except ImportError:
    gp          = None
    base        = None
    tools       = None
    creator     = None
    algorithms  = None

class GenericEvaluate():
    
    def __init__(self, early_stopping, threshold, hof=None):
        
        assert gp is not None, "Did not import gp. Is DEAP installed?"
        
        self.toolbox            = None
        
        if hof is None:
            self.hof                = tools.HallOfFame(maxsize=1)  
        else:
            self.hof                = hof
            
        self.early_stopping     = early_stopping
        self.threshold          = threshold
        
    def set_toolbox(self,toolbox):
        
        self.toolbox = toolbox   
            
    def reward(self, individual, f):
        """
            This is called by some derived classes, but does not always need to
            fall in the flow of a derived class. Sometimes it can be ignored. 
        """
        raise NotImplementedError
            
    def __call__(self, individual):
        """
            This needs to be called in a derived task such as gp_regression
        """
        raise NotImplementedError

