import os
curr = os.getcwd()
_repo = 'EnergyEconomicsE2023'
_repodir = os.path.join(os.getcwd().split(_repo,1)[0],_repo)
_pydir = os.path.join(_repodir,'py')
os.chdir(_pydir)
from base import *
from baseSparse import *
os.chdir(curr)
from scipy.stats import norm
from scipy.optimize import fsolve, minimize
from scipy import sparse, linalg
import numpy as np
import warnings

class UserErrorMessage(UserWarning):
	pass

class WritePropertyError(Exception):
    pass

class mSimpleNL(): 
	def __init__(self, db):
		self.db = db.copy()
		mSimpleNL.updateDB(self.db)
		self.endo_vars = {'endo_var':['p'],'theta_var':[]}
		self.idx_endo = {'p': range(0,len(self.db['h']))} 		
		self.set_model_structure()
		self.set_model_parameters()

	###########################################
	# Initialize class
	###########################################

	@staticmethod
	def updateDB(db):
		""" Function for initializing model database """
		db.updateAlias(alias = [('h','h_alias')])
		db.__setitem__('sigma_E',2); 						# Here we include a smoothing parameter as a scalar, but this could also be a scalar.
		db.__setitem__('sigma_L',2);						# Here we include a smoothing parameter as a scalar, but this could also be a scalar.
		db['p'] = pd.Series(0,index=db['h'],name='p')		# Here we initialize the price vector

	def set_model_parameters(self):
		""" This is a function loading in multiple read(-write) model properties (i.e. parameters) """
		self._hourlyGeneratingCapacity = (adjMultiIndex.bc(self.db['GeneratingCapacity'], self.db['id2hvt']) * self.db['CapVariation']).dropna().droplevel('hvt').astype(float)
		self._hourlyDemand_c = (self.db['LoadVariation'] * self.db['Load']).astype(float)
		self._Demand = self.hourlyDemand_c.groupby('h').sum()
		self._fuelCost = self.db['FuelPrice'].add(pyDbs.pdSum(self.db['EmissionIntensity'] * self.db['EmissionTax'], 'EmissionType'), fill_value=0).astype(float)
		self._averageMC = self.db['OtherMC'].add((pyDbs.pdSum((self.db['FuelMix'] * self.fuelCost).dropna(), 'BFt')),fill_value=0).astype(float)
	
	def set_model_structure(self):
		""" This is a function loading in multiple read model properties """
		self._H = len(self.db['h'])
		self._id2h = (adjMultiIndex.bc(self.db['GeneratingCapacity'], self.db['id2hvt']) * self.db['CapVariation']).dropna().droplevel('hvt').index

		
	###########################################
	# Set the parameters of the model 
	# independent of the market equilibrium
	###########################################

	@property
	def hourlyGeneratingCapacity(self):
		""" Hourly generating capacity measured in GJ """
		return self._hourlyGeneratingCapacity

	@hourlyGeneratingCapacity.setter
	def hourlyGeneratingCapacity(self,series):
		self._hourlyGeneratingCapacity = series
		self._id2h = series.index
	
	@property
	def hourlyDemand_c(self):
		""" Hourly demand measured in GJ """
		return self._hourlyDemand_c

	@hourlyDemand_c.setter
	def hourlyDemand_c(self,value):
		self._hourlyDemand_c = series

	@property
	def Demand(self):
		""" Hourly demand measured in GJ """
		return self._Demand

	@Demand.setter
	def Demand(self,value):
		raise WritePropertyError('Demand is read-only and given by hourlyDemand_c.')

	@property
	def fuelCost(self):
		""" Marginal fuel costs in €/GJ """
		return self._fuelCost

	@fuelCost.setter
	def fuelCost(self,series):
		self._fuelCost = series

	@property
	def averageMC(self):
		""" Marginal costs in €/GJ """
		return self._averageMC

	@averageMC.setter
	def averageMC(self,series):
		self._averageMC = series


	###########################################
	# Set the indices of the model, 
	# determined by the data
	###########################################

	@property 
	def H(self):
		return self._H

	@H.setter
	def H(self,multiindex):
		raise WritePropertyError('The number of hours (H) is read-only and determined by the data.')

	@property
	def id2h(self):
		return self._id2h

	@id2h.setter
	def id2h(self,series):
		raise WritePropertyError('The mapping between id and h is always given by the hourly generating capacity.')

	###########################################
	# Function for updating variables post
	# simulations
	###########################################

	def unloadSolutionToDB(self,roots):
		""" Returns af pandas series of x_vars in model database, when model is solved """
		[self.db.__setitem__(i,pd.Series(roots[self.idx_endo[i]],index=self.db[i].index,name=i)) if pyDbs.type_(self.db[i])=='variable' else self.db.__setitem__(i,roots[self.idx_endo[i]]) if pyDbs.type_(self.db[i])=='scalar' else None for i in self.endo_vars['endo_var']+self.endo_vars['theta_var']];

	###########################################
	# Function for changing endogenous numpy 
	# array to pandas series
	###########################################

	def xArray2pdSeries(self,x,variable='p'):
		""" Method for transforming endogenous variables from a numpy array to an indexed Pandas Series """
		if variable in self.endo_vars['endo_var']+self.endo_vars['theta_var']:
			return pd.Series(x[self.idx_endo[variable]],index=self.db[variable].index,name=variable) if (pyDbs.type_(self.db[variable])=='variable') else x[self.idx_endo[variable]] if (pyDbs.type_(self.db[variable])=='scalar') else None
		else:
			return self.db[variable]

	###########################################
	# Model equations
	###########################################

	def Supply(self,x):
		""" Smooth supply function """
		Inner = pd.Series(0,index=self.id2h).add(self.xArray2pdSeries(x,variable='p')).sub(self.averageMC).div(self.db['sigma_E'])
		return (self.hourlyGeneratingCapacity * Inner.apply(norm.cdf)).groupby('h').sum()
	
	def Load(self,x):
		""" Load/demand function"""
		return ((self.db['meanMWP'] - self.xArray2pdSeries(x,variable='p')) / self.db['sigma_L']).apply(norm.cdf) * self.Demand

	def ExcessDemand(self,x):
		""" Equilibrium identity defined as excess demand"""
		return self.Load(x) - self.Supply(x)

	###########################################
	# Define solvers
	###########################################

	def ScipySolver(self,p0=None,maxiter=0):
		""" A wrapper around Scipy's fsolve function"""
		if p0 is None:
			p0 = self.db['p'].values
		sol = fsolve(func=lambda x: self.ExcessDemand(x), x0=p0,full_output=True,maxfev=maxiter)
		print(sol[-1])
		if sol[-1]=='The solution converged.':
			p_star = sol[0]
			self.unloadSolutionToDB(p_star)

###########################################
# NEW CLASS
###########################################

class mEstimateNL(mSimpleNL):
	""" A class for estimating a non-linear energy system model"""
	def __init__(self, db):
		super().__init__(db)
		self.theta_vars = ['sigma_E','OtherMC']
		self.endo_vars = {
			'endo_var':['p'],
			'theta_var':self.theta_vars.copy()
		}
		self.idx_endo = {
			'p': range(0,len(self.db['h'])),
			'sigma_E':len(self.db['h']),
			'OtherMC': range(len(self.db['h']) + 1 , len(self.db['h']) + len(self.db['OtherMC']) + 1)
		}

	###########################################
	# Model equations
	###########################################

	def averageMC(self,x):
		""" average marginal generation costs"""
		return self.xArray2pdSeries(x,variable='OtherMC').add((pyDbs.pdSum((self.xArray2pdSeries(x,variable='FuelMix') * self.fuelCost).dropna(), 'BFt')),fill_value=0).astype(float)	

	def Supply(self,x):
		""" Smooth supply function """
		Inner = pd.Series(0,index=self.id2h).add(self.xArray2pdSeries(x,variable='p')).sub(self.averageMC(x)).div(self.xArray2pdSeries(x,variable='sigma_E')) 
		return (self.hourlyGeneratingCapacity * Inner.apply(norm.cdf)).groupby('h').sum()
	
	###########################################
	# Define estimators
	###########################################

	def logLikelihood(self,x,data):
		""" log-likehood objective function in estimation algorithm"""
		error = norm.pdf(data-x[self.idx_endo['p']])
		return -np.log(np.where(error==0,np.finfo(float).eps,error)).sum()

	def ConstrainedMLE(self,data,theta0=None,p0=None,maxiter=100,constraints=None):
		"""Estimation algorithm formulated as a constrained Maximum Likelihood Estimation (MLE)"""
		# Initial guess of parameter vector
		if theta0 is None:
			theta0 = np.concatenate([self.db[var].values.astype(float) if pyDbs.type_(self.db[var])=='variable' else np.array([float(self.db[var])]) if pyDbs.type_(self.db[var])=='scalar' else None for var in self.endo_vars['theta_var']]).ravel().astype(float)
		# Initial guess of price vector
		if p0 is None:
			p0 = self.db['p'].values
		x0 = np.append(p0,theta0)
		# Solution
		sol = minimize(
			fun = lambda x: self.logLikelihood(x,data=data),
			x0 = x0,
			constraints = [{'type':'eq','fun':self.ExcessDemand}],
			options = {'maxiter':maxiter}
		)
		print(sol['message'])
		if sol['success']:
			self.x = sol['x']
			self.theta_hat = sol['x'][range(len(self.db['h']),max(self.idx_endo[self.endo_vars['theta_var'][-1]])+1)]
			self.unloadSolutionToDB(self.x)


