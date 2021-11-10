######################################################################
# BioSimSpace: Making biomolecular simulation a breeze!
#
# Copyright: 2017-2021
#
# Authors: Lester Hedges <lester.hedges@gmail.com>
#
# BioSimSpace is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with BioSimSpace. If not, see <http://www.gnu.org/licenses/>.
#####################################################################

"""
Functionality for running simulations with SOMD.
"""

__author__ = "Lester Hedges"
__email__ = "lester.hedges@gmail.com"

__all__ = ["Somd"]

import math as _math
import os as _os
import pygtail as _pygtail
import random as _random
import string as _string
import sys as _sys
import timeit as _timeit
import warnings as _warnings

from Sire import Base as _SireBase
from Sire import CAS as _SireCAS
from Sire import IO as _SireIO
from Sire import MM as _SireMM
from Sire import Mol as _SireMol

from BioSimSpace import _isVerbose
from BioSimSpace._Exceptions import IncompatibleError as _IncompatibleError
from BioSimSpace._Exceptions import MissingSoftwareError as _MissingSoftwareError
from BioSimSpace._SireWrappers import Molecule as _Molecule
from BioSimSpace._SireWrappers import System as _System

from BioSimSpace import IO as _IO
from BioSimSpace import Protocol as _Protocol
from BioSimSpace import Trajectory as _Trajectory
from BioSimSpace import _Utils as _Utils

from . import _process

class Somd(_process.Process):
    """A class for running simulations using SOMD."""

    # Dictionary of platforms and their OpenMM keyword.
    _platforms = { "CPU"    : "CPU",
                   "CUDA"   : "CUDA",
                   "OPENCL" : "OpenCL" }

    def __init__(self, system, protocol, exe=None, name="somd",
            platform="CPU", work_dir=None, seed=None, property_map={}):
        """Constructor.

           Parameters
           ----------

           system : :class:`System <BioSimSpace._SireWrappers.System>`
               The molecular system.

           protocol : :class:`Protocol <BioSimSpace.Protocol>`
               The protocol for the SOMD process.

           exe : str
               The full path to the SOMD executable.

           name : str
               The name of the process.

           platform : str
               The platform for the simulation: "CPU", "CUDA", or "OPENCL".

           work_dir :
               The working directory for the process.

           seed : int
               A random number seed. Note that SOMD only uses a seed for
               FreeEnergy protocols. The seed should only be used for debugging
               purposes since SOMD uses the same seed for each Monte Carlo
               cycle.

           property_map : dict
               A dictionary that maps system "properties" to their user defined
               values. This allows the user to refer to properties with their
               own naming scheme, e.g. { "charge" : "my-charge" }
        """

        # Call the base class constructor.
        super().__init__(system, protocol, name, work_dir, seed, property_map)

        # Set the package name.
        self._package_name = "SOMD"

        # This process can generate trajectory data.
        self._has_trajectory = True

        if type(platform) is not str:
            raise TypeError("'platform' must be of type 'str'.")
        else:
            # Strip all whitespace and convert to upper case.
            platform = platform.replace(" ", "").upper()

            # Check for platform support.
            if platform not in self._platforms:
                raise ValueError("Supported platforms are: %s" % self._platforms.keys())
            else:
                self._platform = self._platforms[platform]

        # If the path to the executable wasn't specified, then use the bundled SOMD
        # executable.
        if exe is None:
            # Generate the name of the SOMD exe.
            if _sys.platform != "win32":
                somd_path = _SireBase.getBinDir()
                somd_suffix = ""
            else:
                somd_path = _os.path.join(_os.path.normpath(_SireBase.getShareDir()), "scripts")
                somd_interpreter = _os.path.join(_os.path.normpath(_SireBase.getBinDir()), "sire_python.exe")
                somd_suffix = ".py"
            if type(self._protocol) is _Protocol.FreeEnergy:
                somd_exe = "somd-freenrg"
            else:
                somd_exe = "somd"
            somd_exe = _os.path.join(somd_path, somd_exe) + somd_suffix
            if not _os.path.isfile(somd_exe):
                raise _MissingSoftwareError("'Cannot find SOMD executable in expected location: '%s'" % somd_exe)
            if _sys.platform != "win32":
                self._exe = somd_exe
            else:
                self._exe = somd_interpreter
                self._script = somd_exe
        else:
            # Make sure executable exists.
            if _os.path.isfile(exe):
                self._exe = exe
            else:
                raise IOError("SOMD executable doesn't exist: '%s'" % exe)

        # The names of the input files.
        self._rst_file = "%s/%s.rst7" % (self._work_dir, name)
        self._top_file = "%s/%s.prm7" % (self._work_dir, name)

        # The name of the trajectory file.
        self._traj_file = "%s/traj000000001.dcd" % self._work_dir

        # The name of the restart file.
        self._restart_file = "%s/latest.pdb" % self._work_dir

        # Set the path for the SOMD configuration file.
        self._config_file = "%s/%s.cfg" % (self._work_dir, name)

        # Set the path for the perturbation file.
        self._pert_file = "%s/%s.pert" % (self._work_dir, name)

        # Set the path for the gradient file and create the gradient list.
        self._gradient_file = "%s/gradients.dat" % self._work_dir
        self._gradients = []

        # Create the list of input files.
        self._input_files = [self._config_file, self._rst_file, self._top_file]

        # Initialise the number of moves per cycle.
        self._num_moves = 10000

        # Initialise the buffering frequency.
        self._buffer_freq = 0

        # Now set up the working directory for the process.
        self._setup()

    def __str__(self):
        """Return a human readable string representation of the object."""
        return "<BioSimSpace.Process.%s: system=%s, protocol=%s, exe='%s', name='%s', platform='%s', work_dir='%s' seed=%s>" \
            % (self.__class__.__name__, str(self._system), self._protocol.__repr__(),
               self._exe + ("%s " % self._script if self._script else ""),
               self._name, self._platform, self._work_dir, self._seed)

    def __repr__(self):
        """Return a string showing how to instantiate the object."""
        return "BioSimSpace.Process.%s(%s, %s, exe='%s', name='%s', platform='%s', work_dir='%s', seed=%s)" \
            % (self.__class__.__name__, str(self._system), self._protocol.__repr__(),
               self._exe + ("%s " % self._script if self._script else ""),
               self._name, self._platform, self._work_dir, self._seed)

    def _setup(self):
        """Setup the input files and working directory ready for simulation."""

        # Create the input files...

        # First create a copy of the system.
        system = self._system.copy()

        # If the we are performing a free energy simulation, then check that
        # the system contains a single perturbable molecule. If so, then create
        # and write a perturbation file to the work directory.
        if type(self._protocol) is _Protocol.FreeEnergy:
            if system.nPerturbableMolecules() == 1:
                # Extract the perturbable molecule.
                pert_mol = system.getPerturbableMolecules()[0]

                # Write the perturbation file and get the molecule corresponding
                # to the lambda = 0 state.
                pert_mol = _to_pert_file(pert_mol, self._pert_file, property_map=self._property_map,
                                         perturbation_type=self._protocol.getPerturbationType())

                self._input_files.append(self._pert_file)

                # Remove the perturbable molecule.
                system._sire_object.remove(pert_mol.number())

                # Recreate the system, putting the perturbable molecule with
                # renamed properties first.
                updated_system = _System(pert_mol) + _System(system)

                # Copy across all of the properties from the original system.
                for prop in system._sire_object.propertyKeys():
                    updated_system._sire_object.setProperty(prop, system._sire_object.property(prop))

                # Copy the updated system object across.
                system = updated_system

            else:
                raise ValueError("'BioSimSpace.Protocol.FreeEnergy' requires a single "
                                 "perturbable molecule. The system has %d" \
                                  % system.nPerturbableMolecules())

        # If this is a different protocol and the system still contains a
        # perturbable molecule, then we'll warn the user and simulate the
        # lambda = 0 state.
        else:
            system = self._checkPerturbable(system)

        # Convert the water model topology so that it matches the AMBER naming convention.
        system._set_water_topology("AMBER", self._property_map)

        # RST file (coordinates).
        try:
            rst = _SireIO.AmberRst7(system._sire_object, self._property_map)
            rst.writeToFile(self._rst_file)
        except Exception as e:
            msg = "Failed to write system to 'RST7' format."
            if _isVerbose():
                raise IOError(msg) from e
            else:
                raise IOError(msg) from None

        # PRM file (topology).
        try:
            prm = _SireIO.AmberPrm(system._sire_object, self._property_map)
            prm.writeToFile(self._top_file)
        except Exception as e:
            msg = "Failed to write system to 'PRM7' format."
            if _isVerbose():
                raise IOError(msg) from e
            else:
                raise IOError(msg) from None

        # Generate the SOMD configuration file.
        # Skip if the user has passed a custom config.
        if type(self._protocol) is _Protocol.Custom:
            self.setConfig(self._protocol.getConfig())
        else:
            self._generate_config()
        self.writeConfig(self._config_file)

        # Generate the dictionary of command-line arguments.
        self._generate_args()

        # Return the list of input files.
        return self._input_files

    def _generate_config(self):
        """Generate SOMD configuration file strings."""

        # Clear the existing configuration list.
        self._config = []

        # Check whether the system contains periodic box information.
        # For now, well not attempt to generate a box if the system property
        # is missing. If no box is present, we'll assume a non-periodic simulation.
        if "space" in self._system._sire_object.propertyKeys():
            has_box = True
        else:
            _warnings.warn("No simulation box found. Assuming gas phase simulation.")
            has_box = False

        # Work out the GPU device ID. (Default to 0.)
        gpu_id = 0
        if self._platform == "CUDA":
            if "CUDA_VISIBLE_DEVICES" in _os.environ:
                try:
                    # Get the ID of the first available device.
                    #gpu_id = int(_os.environ.get("CUDA_VISIBLE_DEVICES").split(",")[0])
                    gpu_id = 0
                except:
                    raise EnvironmentError("'CUDA' platform is selected but cannot parse "
                                           "'CUDA_VISIBLE_DEVICES' environment variable!")
            else:
                raise EnvironmentError("'CUDA' platform selected but 'CUDA_VISIBLE_DEVICES' "
                                       "environment variable is unset.")

        # While the configuration parameters below share a lot of overlap,
        # we choose the keep them separate so that the user can modify options
        # for a given protocol in a single place.

        # Add configuration variables for a minimisation simulation.
        if type(self._protocol) is _Protocol.Minimisation:
            if self._platform == "CUDA" or self._platform == "OPENCL":
                self.addToConfig("gpu = %d" % gpu_id)                   # GPU device ID.
            self.addToConfig("minimise = True")                         # Minimisation simulation.
            self.addToConfig("minimise maximum iterations = %d"         # Maximum number of steps.
                % self._protocol.getSteps())
            self.addToConfig("minimise tolerance = 1")                  # Convergence tolerance.
            self.addToConfig("ncycles = 1")                             # Perform a single SOMD cycle.
            self.addToConfig("nmoves = 1")                              # Perform a single MD move.
            self.addToConfig("save coordinates = True")                 # Save molecular coordinates.
            if not has_box or not self._has_water:
                self.addToConfig("cutoff type = cutoffnonperiodic")     # No periodic box.
            else:
                self.addToConfig("cutoff type = cutoffperiodic")        # Periodic box.
            self.addToConfig("cutoff distance = 10 angstrom")           # Non-bonded cut-off.
            if not has_box:
                self.addToConfig("barostat = False")                    # Disable barostat if no simulation box.

        # In the following protocols we save coordinates every cycle, which is
        # 10000 MD steps (moves) in length (this is for consistency with other
        # MD drivers).

        # Add configuration variables for an equilibration simulation.
        elif type(self._protocol) is _Protocol.Equilibration:
            # Only constant temperature equilibration simulations are supported.
            if not self._protocol.isConstantTemp():
                raise _IncompatibleError("SOMD only supports constant temperature equilibration.")

            # Restraints aren't supported.
            if self._protocol.getRestraint() is not None:
                raise _IncompatibleError("We currently don't support restraints with SOMD.")

            # Get the report and restart intervals.
            report_interval = self._protocol.getReportInterval()
            restart_interval = self._protocol.getRestartInterval()

            # Work out the number of cycles.
            ncycles = (self._protocol.getRunTime() / self._protocol.getTimeStep()) / report_interval

            # If the number of cycles isn't integer valued, adjust the report
            # interval so that we match specified the run time.
            if ncycles - _math.floor(ncycles) != 0:
                ncycles = _math.floor(ncycles)
                report_interval = _math.ceil((self._protocol.getRunTime()
                                / self._protocol.getTimeStep()) / ncycles)

            # Work out the number of cycles per frame.
            cycles_per_frame = restart_interval / report_interval

            # Work out whether we need to adjust the buffer frequency.
            buffer_freq = 0
            if cycles_per_frame < 1:
                buffer_freq = cycles_per_frame * restart_interval
                cycles_per_frame = 1
                self._buffer_freq = buffer_freq
            else:
                cycles_per_frame = _math.floor(cycles_per_frame)

            # Convert the timestep to femtoseconds.
            timestep = self._protocol.getTimeStep().femtoseconds().magnitude()

            # Convert the temperature to Kelvin.
            temperature = self._protocol.getStartTemperature().kelvin().magnitude()

            if self._platform == "CUDA" or self._platform == "OPENCL":
                self.addToConfig("gpu = %d" % gpu_id)                               # GPU device ID.
            self.addToConfig("ncycles = %d" % ncycles)                              # The number of SOMD cycles.
            self.addToConfig("nmoves = %d" % report_interval)                       # The number of moves per cycle.
            self.addToConfig("save coordinates = True")                             # Save molecular coordinates.
            self.addToConfig("ncycles_per_snap = %d" % cycles_per_frame)            # Cycles per trajectory write.
            self.addToConfig("buffered coordinates frequency = %d" % buffer_freq)   # Buffering frequency.
            self.addToConfig("timestep = %.2f femtosecond" % timestep)              # Integration time step.
            self.addToConfig("thermostat = True")                                   # Turn on the thermostat.
            self.addToConfig("temperature = %.2f kelvin" % temperature)             # System temperature.
            if self._protocol.getPressure() is None:
                self.addToConfig("barostat = False")                                # Disable barostat (constant volume).
            else:
                if self._has_water and has_box:
                    self.addToConfig("barostat = True")                             # Enable barostat.
                    self.addToConfig("pressure = %.5f atm"                          # Pressure in atmosphere.
                        % self._protocol.getPressure().atm().magnitude())
                else:
                    self.addToConfig("barostat = False")                            # Disable barostat (constant volume).
            if self._has_water:
                self.addToConfig("reaction field dielectric = 78.3")                # Solvated box.
            if not has_box or not self._has_water:
                self.addToConfig("cutoff type = cutoffnonperiodic")                 # No periodic box.
            else:
                self.addToConfig("cutoff type = cutoffperiodic")                    # Periodic box.
            self.addToConfig("cutoff distance = 10 angstrom")                       # Non-bonded cut-off.
            if self._is_seeded:
                self.addToConfig("random seed = %d" % self._seed)                   # Random number seed.

        # Add configuration variables for a production simulation.
        elif type(self._protocol) is _Protocol.Production:

            # Get the report and restart intervals.
            report_interval = self._protocol.getReportInterval()
            restart_interval = self._protocol.getRestartInterval()

            # Work out the number of cycles.
            ncycles = (self._protocol.getRunTime() / self._protocol.getTimeStep()) / report_interval

            # If the number of cycles isn't integer valued, adjust the report
            # interval so that we match specified the run time.
            if ncycles - _math.floor(ncycles) != 0:
                ncycles = _math.floor(ncycles)
                report_interval = _math.ceil((self._protocol.getRunTime()
                                / self._protocol.getTimeStep()) / ncycles)

            # Work out the number of cycles per frame.
            cycles_per_frame = restart_interval / report_interval

            # Work out whether we need to adjust the buffer frequency.
            buffer_freq = 0
            if cycles_per_frame < 1:
                buffer_freq = cycles_per_frame * restart_interval
                cycles_per_frame = 1
                self._buffer_freq = buffer_freq
            else:
                cycles_per_frame = _math.floor(cycles_per_frame)

            # Convert the timestep to femtoseconds.
            timestep = self._protocol.getTimeStep().femtoseconds().magnitude()

            # Convert the temperature to Kelvin.
            temperature = self._protocol.getTemperature().kelvin().magnitude()

            if self._platform == "CUDA" or self._platform == "OPENCL":
                self.addToConfig("gpu = %d" % gpu_id)                               # GPU device ID.
            self.addToConfig("ncycles = %d" % ncycles)                              # The number of SOMD cycles.
            self.addToConfig("nmoves = %d" % report_interval)                       # The number of moves per cycle.
            self.addToConfig("save coordinates = True")                             # Save molecular coordinates.
            self.addToConfig("ncycles_per_snap = %d" % cycles_per_frame)            # Cycles per trajectory write.
            self.addToConfig("buffered coordinates frequency = %d" % buffer_freq)   # Buffering frequency.
            self.addToConfig("timestep = %.2f femtosecond" % timestep)              # Integration time step.
            self.addToConfig("thermostat = True")                                   # Turn on the thermostat.
            self.addToConfig("temperature = %.2f kelvin" % temperature)             # System temperature.
            if self._protocol.getPressure() is None:
                self.addToConfig("barostat = False")                                # Disable barostat (constant volume).
            else:
                if self._has_water and has_box:
                    self.addToConfig("barostat = True")                             # Enable barostat.
                    self.addToConfig("pressure = %.5f atm"                          # Presure in atmosphere.
                        % self._protocol.getPressure().atm().magnitude())
                else:
                    self.addToConfig("barostat = False")                            # Disable barostat (constant volume).
            if self._has_water:
                self.addToConfig("reaction field dielectric = 78.3")                # Solvated box.
            if not has_box or not self._has_water:
                self.addToConfig("cutoff type = cutoffnonperiodic")                 # No periodic box.
            else:
                self.addToConfig("cutoff type = cutoffperiodic")                    # Periodic box.
            self.addToConfig("cutoff distance = 10 angstrom")                       # Non-bonded cut-off.
            if self._is_seeded:
                self.addToConfig("random seed = %d" % self._seed)                   # Random number seed.

        # Add configuration variables for a free energy simulation.
        elif type(self._protocol) is _Protocol.FreeEnergy:

            # Get the report and restart intervals.
            report_interval = self._protocol.getReportInterval()
            restart_interval = self._protocol.getRestartInterval()

            # Work out the number of cycles.
            ncycles = (self._protocol.getRunTime() / self._protocol.getTimeStep()) / report_interval

            # If the number of cycles isn't integer valued, adjust the report
            # interval so that we match specified the run time.
            if ncycles - _math.floor(ncycles) != 0:
                ncycles = _math.floor(ncycles)
                report_interval = _math.ceil((self._protocol.getRunTime()
                                / self._protocol.getTimeStep()) / ncycles)

            # The report interval must be a multiple of the energy frequency,
            # which is 250 steps.
            if report_interval % 250 != 0:
                report_interval = 250 * _math.ceil(report_interval / 250)

            # Work out the number of cycles per frame.
            cycles_per_frame = restart_interval / report_interval

            # Work out whether we need to adjust the buffer frequency.
            buffer_freq = 0
            if cycles_per_frame < 1:
                buffer_freq = cycles_per_frame * restart_interval
                cycles_per_frame = 1
                self._buffer_freq = buffer_freq
            else:
                cycles_per_frame = _math.floor(cycles_per_frame)

            # The buffer frequency must be an integer multiple of the frequency
            # at which free energies are written, which is 250 steps. Round down
            # to the closest multiple.
            if buffer_freq > 0:
                buffer_freq = 250 * _math.floor(buffer_freq / 250)

            # Convert the timestep to femtoseconds.
            timestep = self._protocol.getTimeStep().femtoseconds().magnitude()

            # Convert the temperature to Kelvin.
            temperature = self._protocol.getTemperature().kelvin().magnitude()

            if self._platform == "CUDA" or self._platform == "OPENCL":
                self.addToConfig("gpu = %d" % gpu_id)                               # GPU device ID.
            self.addToConfig("ncycles = %d" % ncycles)                              # The number of SOMD cycles.
            self.addToConfig("nmoves = %d" % report_interval)                       # The number of moves per cycle.
            self.addToConfig("energy frequency = 250")                              # Frequency of free energy gradient evaluation.
            self.addToConfig("save coordinates = True")                             # Save molecular coordinates.
            self.addToConfig("ncycles_per_snap = %d" % cycles_per_frame)            # Cycles per trajectory write.
            self.addToConfig("buffered coordinates frequency = %d" % buffer_freq)   # Buffering frequency.
            self.addToConfig("timestep = %.2f femtosecond" % timestep)              # Integration time step.
            self.addToConfig("thermostat = True")                                   # Turn on the thermostat.
            self.addToConfig("temperature = %.2f kelvin" % temperature)             # System temperature.
            if self._protocol.getPressure() is None:
                self.addToConfig("barostat = False")                                # Disable barostat (constant volume).
            else:
                if self._has_water and has_box:
                    self.addToConfig("barostat = True")                             # Enable barostat.
                    self.addToConfig("pressure = %.5f atm"                          # Presure in atmosphere.
                        % self._protocol.getPressure().atm().magnitude())
                else:
                    self.addToConfig("barostat = False")                            # Disable barostat (constant volume).
            if self._has_water:
                self.addToConfig("reaction field dielectric = 78.3")                # Solvated box.
            if not has_box or not self._has_water:
                self.addToConfig("cutoff type = cutoffnonperiodic")                 # No periodic box.
            else:
                self.addToConfig("cutoff type = cutoffperiodic")                    # Periodic box.
            self.addToConfig("cutoff distance = 10 angstrom")                       # Non-bonded cut-off.
            if self._is_seeded:
                self.addToConfig("random seed = %d" % self._seed)                   # Random number seed.
            self.addToConfig("constraint = hbonds-notperturbed")                    # Handle hydrogen perturbations.
            self.addToConfig("minimise = True")                                     # Perform a minimisation.
            self.addToConfig("equilibrate = False")                                 # Don't equilibrate.
                                                                                    # The lambda value array.
            self.addToConfig("lambda array = %s" \
                % ", ".join([str(x) for x in self._protocol.getLambdaValues()]))
            self.addToConfig("lambda_val = %s" \
                % self._protocol.getLambda())                               # The value of lambda.

        else:
            raise _IncompatibleError("Unsupported protocol: '%s'" % self._protocol.__class__.__name__)

        # Flag that this isn't a custom protocol.
        self._protocol._setCustomised(False)

    def _generate_args(self):
        """Generate the dictionary of command-line arguments."""

        # Clear the existing arguments.
        self.clearArgs()

        # Add the default arguments.
        self.setArg("-c", "%s.rst7" % self._name)                       # Coordinate restart file.
        self.setArg("-t", "%s.prm7" % self._name)                       # Topology file.
        if type(self._protocol) is _Protocol.FreeEnergy:
            self.setArg("-m", "%s.pert" % self._name)                   # Perturbation file.
        self.setArg("-C", "%s.cfg" % self._name)                        # Config file.
        self.setArg("-p", self._platform)                               # Simulation platform.

    def start(self):
        """Start the SOMD process.

           Returns
           -------

           process : :class:`Process.Somd <BioSimSpace.Process.Somd>`
               A handle to the running process.
        """

        # The process is currently queued.
        if self.isQueued():
            return

        # Process is already running.
        if self._process is not None:
            if self._process.isRunning():
                return

        # Clear any existing output.
        self._clear_output()

        # Run the process in the working directory.
        with _Utils.cd(self._work_dir):

            # Create the arguments string list.
            args = self.getArgStringList()

            # Write the command-line process to a README.txt file.
            with open("README.txt", "w") as f:

                # Set the command-line string.
                self._command = "%s " % self._exe + self.getArgString()

                # Write the command to file.
                f.write("# SOMD was run with the following command:\n")
                f.write("%s\n" % self._command)

            # Start the timer.
            self._timer = _timeit.default_timer()

            # Start the simulation.
            self._process = _SireBase.Process.run(self._exe, args,
                "%s.out"  % self._name, "%s.out"  % self._name)

            # SOMD uses the stdout stream for all output.
            with open(_os.path.basename(self._stderr_file), "w") as f:
                f.write("All output has been redirected to the stdout stream!\n")

        return self

    def getSystem(self, block="AUTO"):
        """Get the latest molecular system.

           Parameters
           ----------

           block : bool
               Whether to block until the process has finished running.

           Returns
           -------

           system : :class:`System <BioSimSpace._SireWrappers.System>`
               The latest molecular system.
        """

        # Wait for the process to finish.
        if block is True:
            self.wait()
        elif block == "AUTO" and self._is_blocked:
            self.wait()

        # Warn the user if the process has exited with an error.
        if self.isError():
            _warnings.warn("The process exited with an error!")

        # Try to grab the latest coordinates from the binary restart file.
        try:
            new_system = _IO.readMolecules(self._restart_file)

            # Since SOMD requires specific residue and water naming we copy the
            # coordinates back into the original system.
            old_system = self._system.copy()

            old_system = self._updateCoordinatesAndVelocities(old_system,
                                                              new_system,
                                                              self._property_map,
                                                              self._property_map)

            # Update the box information in the original system.
            if "space" in new_system._sire_object.propertyKeys():
                box = new_system._sire_object.property("space")
                old_system._sire_object.setProperty(self._property_map.get("space", "space"), box)

            return old_system

        except:
            return None

    def getCurrentSystem(self):
        """Get the latest molecular system.

           Returns
           -------

           system : :class:`System <BioSimSpace._SireWrappers.System>`
               The latest molecular system.
        """
        return self.getSystem(block=False)

    def getTrajectory(self, block="AUTO"):
        """Return a trajectory object.

           Parameters
           ----------

           block : bool
               Whether to block until the process has finished running.

           Returns
           -------

           trajectory : :class:`Trajectory <BioSimSpace.Trajectory.trajectory>`
               The latest trajectory object.
        """

        # Wait for the process to finish.
        if block is True:
            self.wait()
        elif block == "AUTO" and self._is_blocked:
            self.wait()

        # Warn the user if the process has exited with an error.
        if self.isError():
            _warnings.warn("The process exited with an error!")

        try:
            return _Trajectory.Trajectory(process=self)

        except:
            return None

    def getFrame(self, index):
        """Return a specific trajectory frame.

           Parameters
           ----------

           index : int
               The index of the frame.

           Returns
           -------

           frame : :class:`System <BioSimSpace._SireWrappers.System>`
               The System object of the corresponding frame.
        """

        if type(index) is not int:
            raise TypeError("'index' must be of type 'int'")

        max_index = int((self._protocol.getRunTime() / self._protocol.getTimeStep())
                  / self._protocol.getRestartInterval()) - 1

        if index < 0 or index > max_index:
            raise ValueError(f"'index' must be in range [0, {max_index}].")

        try:
            new_system =  _Trajectory.getFrame(self._traj_file,
                                               self._top_file,
                                               index)

            # Copy the new coordinates back into the original system.
            old_system = self._system.copy()
            old_system._updateCoordinates(new_system,
                                          self._property_map,
                                          self._property_map)

            # Update the box information in the original system.
            if "space" in new_system._sire_object.propertyKeys():
                box = new_system._sire_object.property("space")
                old_system._sire_object.setProperty(self._property_map.get("space", "space"), box)

            return old_system

        except:
            return None

    def getTime(self, time_series=False, block="AUTO"):
        """Get the time (in nanoseconds).

           Parameters
           ----------

           time_series : bool
               Whether to return a list of time series records.

           block : bool
               Whether to block until the process has finished running.

           Returns
           -------

           time : :class:`Time <BioSimSpace.Types.Time>`
               The current simulation time in nanoseconds.
        """

        # Warn the user if the process has exited with an error.
        if self.isError():
            _warnings.warn("The process exited with an error!")

        # No time records for minimisation protocols.
        if type(self._protocol) is _Protocol.Minimisation:
            return None

        # Get the number of trajectory frames.
        num_frames = self.getTrajectory(block=block).nFrames()

        if num_frames == 0:
            return None

        try:
            # Create the list of time records.
            times = [(self._protocol.getRestartInterval() * self._protocol.getTimeStep()._default_unit()) * x for x in range(1, num_frames + 1)]
        except:
            return None

        if time_series:
            return times
        else:
            return times[-1]

    def getCurrentTime(self, time_series=False):
        """Get the current time (in nanoseconds).

           Parameters
           ----------

           time_series : bool
               Whether to return a list of time series records.

           Returns
           -------

           time : :class:`Time <BioSimSpace.Types.Time>`
               The current simulation time in nanoseconds.
        """
        return self.getTime(time_series, block=False)

    def getGradient(self, time_series=False, block="AUTO"):
        """Get the free energy gradient.

           Parameters
           ----------

           time_series : bool
               Whether to return a list of time series records.

           block : bool
               Whether to block until the process has finished running.

           Returns
           -------

           gradient : float
               The free energy gradient.
        """

        # Wait for the process to finish.
        if block is True:
            self.wait()
        elif block == "AUTO" and self._is_blocked:
            self.wait()

        # Warn the user if the process has exited with an error.
        if self.isError():
            _warnings.warn("The process exited with an error!")

        # No gradient file.
        if not _os.path.isfile(self._gradient_file):
            return None

        # Append any new lines to the gradients list.
        for line in _pygtail.Pygtail(self._gradient_file):
            # Ignore comments.
            if line[0] != "#":
                self._gradients.append(float(line.rstrip().split()[-1]))

        if len(self._gradients) == 0:
            return None

        if time_series:
            return self._gradients
        else:
            return self._gradients[-1]

    def getCurrentGradient(self, time_series=False):
        """Get the current free energy gradient.

           Parameters
           ----------

           time_series : bool
               Whether to return a list of time series records.

           Returns
           -------

           gradient : float
               The current free energy gradient.
        """
        return self.getGradient(time_series, block=False)

    def _clear_output(self):
        """Reset stdout and stderr."""

        # Call the base class method.
        super()._clear_output()

        # Delete any restart and trajectory files in the working directory.

        file = "%s/sim_restart.s3" % self._work_dir
        if _os.path.isfile(file):
            _os.remove(file)

        file = "%s/SYSTEM.s3" % self._work_dir
        if _os.path.isfile(file):
            _os.remove(file)

        files = _IO.glob("%s/traj*.dcd" % self._work_dir)
        for file in files:
            if _os.path.isfile(file):
                _os.remove(file)

        # Additional files for free energy simulations.
        if type(self._protocol) is _Protocol.FreeEnergy:

            file = "%s/gradients.dat" % self._work_dir
            if _os.path.isfile(file):
                _os.remove(file)

            file = "%s/simfile.dat" % self._work_dir
            if _os.path.isfile(file):
                _os.remove(file)

    def _updateCoordinatesAndVelocities(self, old_system, new_system,
            property_map0={}, property_map1={}, is_lambda1=False):
        """Update the coordinates and velocities of atoms in the system.

           Parameters
           ----------

           old_system : :class:`System <BioSimSpace._SireWrappers.System>`
               The original molecular system.

           new_system : :class:`System <BioSimSpace._SireWrappers.System>`
               A system containing the updated coordinates.

           property_map0 : dict
               A dictionary that maps system "properties" to their user defined
               values in this system.

           property_map1 : dict
               A dictionary that maps system "properties" to their user defined
               values in the passed system.

           is_lambda1 : bool
              Whether to update coordinates of perturbed molecules at lambda = 1.
              By default, coordinates at lambda = 0 are used.
        """

        # Rather than using the _updateCoordinates of the system directly,
        # this is a custom function for SOMD since it doesn't preserve
        # molecular ordering on startup.

        # Validate the systems.

        if type(old_system) is not _System:
            raise TypeError("'old_system' must be of type 'BioSimSpace._SireWrappers.System'")

        if type(new_system) is not _System:
            raise TypeError("'new_system' must be of type 'BioSimSpace._SireWrappers.System'")

        # Check that the two systems contain the same number of molecules.
        if old_system.nMolecules() != new_system.nMolecules():
            raise _IncompatibleError("The two systems contains a different number of "
                                     "molecules. Expected '%d', found '%d'"
                                     % (old_system.nMolecules(), new_system.nMolecules()))

        if type(property_map0) is not dict:
            raise TypeError("'property_map0' must be of type 'dict'.")

        if type(property_map1) is not dict:
            raise TypeError("'property_map1' must be of type 'dict'.")

        if type(is_lambda1) is not bool:
            raise TypeError("'is_lambda1' must be of type 'bool'.")

        # Work out the name of the "coordinates" property.
        prop_c0 = property_map0.get("coordinates0", "coordinates")
        prop_c1 = property_map1.get("coordinates1", "coordinates")

        # Work out the name of the "velocity" property.
        prop_v0 = property_map0.get("velocity", "coordinates")
        prop_v1 = property_map1.get("velocity", "coordinates")

        # Loop over all molecules and update the coordinates.
        for idx, mol0 in enumerate(old_system.getMolecules()):
            # Get the number of the first atom in the molecule.
            num = mol0.getAtoms()[0]._sire_object.number().value()

            # Get the underlying Sire object.
            mol0 = mol0._sire_object

            # Search for the molecule with the same atom number in the new system.
            search = new_system._sire_object.search(f"mol with atomnum {num}")

            if len(search) != 1:
                msg = "Unable to update 'coordinates' for molecule index '%d'" % idx
                if _isVerbose():
                    raise _IncompatibleError(msg) from e
                else:
                    raise _IncompatibleError(msg) from None

            # Extract the matching molecule.
            mol1 = search[0]

            # Check whether the molecule is perturbable.
            if mol0.hasProperty("is_perturbable"):
                if is_lambda1:
                    prop_c = "coordinates1"
                else:
                    prop_c = "coordinates0"
            else:
                prop_c = prop_c0

            # Try to update the coordinates property.
            try:
                mol0 = mol0.edit().setProperty(prop_c, mol1.property(prop_c1)).molecule().commit()
            except Exception as e:
                msg = "Unable to update 'coordinates' for molecule index '%d'" % idx
                if _isVerbose():
                    raise _IncompatibleError(msg) from e
                else:
                    raise _IncompatibleError(msg) from None

            # Try to update the velocity property. This isn't always present so
            # only try this when the passed system contains the property.
            if mol1.hasProperty(prop_v1):
                try:
                    mol0 = mol0.edit().setProperty(prop_v0, mol1.property(prop_v1)).molecule().commit()
                except Exception as e:
                    msg = "Unable to update 'velocity' for molecule index '%d'" % idx
                    if _isVerbose():
                        raise _IncompatibleError(msg) from e
                    else:
                        raise _IncompatibleError(msg) from None

            # Update the molecule in the original system.
            old_system._sire_object.update(mol0)

        return old_system

def _to_pert_file(molecule, filename="MORPH.pert", zero_dummy_dihedrals=False,
        zero_dummy_impropers=False, print_all_atoms=False, property_map={},
        perturbation_type="full"):
    """Write a perturbation file for a perturbable molecule.

        Parameters
        ----------

        molecule : BioSimSpace._SireWrappers.Molecule
            The perturbable molecule.

        filename : str
            The name of the perturbation file.

        zero_dummy_dihedrals : bool
            Whether to zero the barrier height for dihedrals involving
            dummy atoms.

        zero_dummy_impropers : bool
            Whether to zero the barrier height for impropers involving
            dummy atoms.

        print_all_atoms : bool
            Whether to print all atom records to the pert file, not just
            the atoms that are perturbed.

        property_map : dict
            A dictionary that maps system "properties" to their user defined
            values. This allows the user to refer to properties with their
            own naming scheme, e.g. { "charge" : "my-charge" }

        perturbation_type : str
            The type of perturbation to perform. Options are:
            "full" : A full perturbation of all terms (default option).
            "discharge_soft" : Perturb all discharging soft atom charge terms (i.e. value->0.0).
            "vanish_soft" : Perturb all vanishing soft atom LJ terms (i.e. value->0.0).
            "flip" : Perturb all hard atom terms as well as bonds/angles.
            "grow_soft" : Perturb all growing soft atom LJ terms (i.e. 0.0->value).
            "charge_soft" : Perturb all charging soft atom LJ terms (i.e. 0.0->value).

        Returns
        -------

        molecule : Sire.Mol.Molecule
            The molecule with properties corresponding to the lamda = 0 state.
    """
    if type(molecule) is not _Molecule:
        raise TypeError("'molecule' must be of type 'BioSimSpace._SireWrappers.Molecule'")

    if not molecule._is_perturbable:
        raise _IncompatibleError("'molecule' isn't perturbable. Cannot write perturbation file!")

    if not molecule._sire_object.property("forcefield0").isAmberStyle():
        raise _IncompatibleError("Can only write perturbation files for AMBER style force fields.")

    if type(zero_dummy_dihedrals) is not bool:
        raise TypeError("'zero_dummy_dihedrals' must be of type 'bool'")

    if type(zero_dummy_impropers) is not bool:
        raise TypeError("'zero_dummy_impropers' must be of type 'bool'")

    if type(print_all_atoms) is not bool:
        raise TypeError("'print_all_atoms' must be of type 'bool'")

    if type(property_map) is not dict:
        raise TypeError("'property_map' must be of type 'dict'")

    if type(perturbation_type) is not str:
        raise TypeError("'perturbation_type' must be of type 'str'")

    # Convert to lower case and strip whitespace.
    perturbation_type = perturbation_type.lower().replace(" ", "")

    allowed_perturbation_types = ["full",
                                    "discharge_soft",
                                    "vanish_soft",
                                    "flip",
                                    "grow_soft",
                                    "charge_soft"]

    if perturbation_type not in allowed_perturbation_types:
        raise ValueError(f"'perturbation_type' must be one of: {allowed_perturbation_types}")

    # Seed the random number generator so that we get reproducible atom names.
    # This is helpful when debugging since we can directly compare pert files.
    _random.seed(42)

    # Extract and copy the Sire molecule.
    mol = molecule._sire_object.__deepcopy__()

    # First work out the indices of atoms that are perturbed.
    pert_idxs = []

    # Perturbed atoms change one of the following properties:
    # "ambertype", "LJ", or "charge".
    for atom in mol.atoms():
        if atom.property("ambertype0") != atom.property("ambertype1") or \
            atom.property("LJ0") != atom.property("LJ1")               or \
            atom.property("charge0") != atom.property("charge1"):
            pert_idxs.append(atom.index())

    # The pert file uses atom names for identification purposes. This means
    # that the names must be unique. As such we need to count the number of
    # atoms with a particular name, then append an index to their name.

    # A dictionary to track the atom names.
    atom_names = {}

    # Loop over all atoms in the molecule.
    for atom in mol.atoms():
        if atom.name() in atom_names:
            atom_names[atom.name()] += 1
        else:
            atom_names[atom.name()] = 1

    # Create a set from the atoms names seen so far.
    names = set(atom_names.keys())

    # If there are duplicate names, then we need to rename the atoms.
    if sum(atom_names.values()) > len(names):

        # Make the molecule editable.
        edit_mol = mol.edit()

        # Create a dictionary to flag whether we've seen each atom name.
        is_seen = { name : False for name in names }

        # Tally counter for the number of dummy atoms.
        num_dummy = 1

        # Loop over all atoms.
        for atom in mol.atoms():
            # Store the original atom.
            name = atom.name()

            # If this is a dummy atom, then rename it as "DU##", where ## is a
            # two-digit number padded with a leading zero.
            if atom.property("element0") == _SireMol.Element("X"):
                # Create the new atom name.
                new_name = "DU%02d" % num_dummy

                # Convert to an AtomName and rename the atom.
                new_name = _SireMol.AtomName(new_name)
                edit_mol = edit_mol.atom(atom.index()).rename(new_name).molecule()

                # Update the number of dummy atoms that have been named.
                num_dummy += 1

                # Since ligands typically have less than 100 atoms, the following
                # exception shouldn't be triggered. We can't support perturbations
                # with 100 or more dummy atoms in the lambda = 0 state because of
                # AMBER fixed width atom naming restrictions (4 character width).
                # We could give dummies a unique name in the same way that non-dummy
                # atoms are handled (see else) block below, but instead we'll raise
                # an exception.
                if num_dummy == 100:
                    raise RuntimeError("Dummy atom naming limit exceeded! (100 atoms)")

                # Append to the list of seen names.
                names.add(new_name)

            else:
                # There is more than one atom with this name, and this is the second
                # time we've come across it.
                if atom_names[name] > 1 and is_seen[name]:
                    # Create the base of the new name.
                    new_name = name.value()

                    # Create a random suffix.
                    suffix = _random_suffix(new_name)

                    # Zero the number of attempted renamings.
                    num_attempts = 0

                    # If this name already exists, keep trying until we get a unique name.
                    while new_name + suffix in names:
                        suffix = _random_suffix(new_name)
                        num_attempts += 1

                        # Abort if we've tried more than 100 times.
                        if num_attempts == 100:
                            raise RuntimeError("Error while writing SOMD pert file. "
                                               "Unable to generate a unique suffix for "
                                               "atom name: '%s'" % new_name)

                    # Append the suffix to the name and store in the set of seen names.
                    new_name = new_name + suffix
                    names.add(new_name)

                    # Convert to an AtomName and rename the atom.
                    new_name = _SireMol.AtomName(new_name)
                    edit_mol = edit_mol.atom(atom.index()).rename(new_name).molecule()

                # Record that we've seen this atom name.
                is_seen[name] = True

        # Store the updated molecule.
        mol = edit_mol.commit()

    # Now write the perturbation file.

    with open(filename, "w") as file:
        # Get the info object for the molecule.
        info = mol.info()

        # Write the version header.
        file.write("version 1\n")

        # Start molecule record.
        file.write("molecule LIG\n")

        if print_all_atoms:
            raise NotImplementedError("print_all_atoms is not allowed during dev of multistep protocol.")

        # 1) Atoms.

        def atom_sorting_criteria(atom):
            LJ0 = atom.property("LJ0");
            LJ1 = atom.property("LJ1");
            return (atom.name().value(),
                    atom.property("ambertype0"),
                    atom.property("ambertype1"),
                    LJ0.sigma().value(),
                    LJ1.sigma().value(),
                    LJ0.epsilon().value(),
                    LJ1.epsilon().value(),
                    atom.property("charge0").value(),
                    atom.property("charge1").value())

        if perturbation_type == "full":
            if print_all_atoms:
                for atom in sorted(mol.atoms(), key=lambda atom: atom_sorting_criteria(atom)):
                    # Start atom record.
                    file.write("    atom\n")

                    # Get the initial/final Lennard-Jones properties.
                    LJ0 = atom.property("LJ0");
                    LJ1 = atom.property("LJ1");

                    # Atom data.
                    file.write("        name           %s\n" % atom.name().value())
                    file.write("        initial_type   %s\n" % atom.property("ambertype0"))
                    file.write("        final_type     %s\n" % atom.property("ambertype1"))
                    file.write("        initial_LJ     %.5f %.5f\n" % (LJ0.sigma().value(), LJ0.epsilon().value()))
                    file.write("        final_LJ       %.5f %.5f\n" % (LJ1.sigma().value(), LJ1.epsilon().value()))
                    file.write("        initial_charge %.5f\n" % atom.property("charge0").value())
                    file.write("        final_charge   %.5f\n" % atom.property("charge1").value())

                    # End atom record.
                    file.write("    endatom\n")

            # Only print records for the atoms that are perturbed.
            else:
                for idx in sorted(pert_idxs, key=lambda idx: atom_sorting_criteria(mol.atom(idx))):
                    # Get the perturbed atom.
                    atom = mol.atom(idx)

                    # Start atom record.
                    file.write("    atom\n")

                    # Get the initial/final Lennard-Jones properties.
                    LJ0 = atom.property("LJ0");
                    LJ1 = atom.property("LJ1");

                    # Atom data.
                    file.write("        name           %s\n" % atom.name().value())
                    file.write("        initial_type   %s\n" % atom.property("ambertype0"))
                    file.write("        final_type     %s\n" % atom.property("ambertype1"))
                    file.write("        initial_LJ     %.5f %.5f\n" % (LJ0.sigma().value(), LJ0.epsilon().value()))
                    file.write("        final_LJ       %.5f %.5f\n" % (LJ1.sigma().value(), LJ1.epsilon().value()))
                    file.write("        initial_charge %.5f\n" % atom.property("charge0").value())
                    file.write("        final_charge   %.5f\n" % atom.property("charge1").value())

                    # End atom record.
                    file.write("    endatom\n")
        else:
            # Given multistep protocol:
            if print_all_atoms:
                raise NotImplementedError("print_all_atoms in multistep approach is not yet implemented.")

            for idx in sorted(pert_idxs, key=lambda idx: atom_sorting_criteria(mol.atom(idx))):
                # Get the perturbed atom.
                atom = mol.atom(idx)
                # Start atom record.
                file.write("    atom\n")

                # Get the initial/final Lennard-Jones properties.
                LJ0 = atom.property("LJ0");
                LJ1 = atom.property("LJ1");

                # Atom data.
                # Get the atom types:
                atom_type0 = atom.property("ambertype0")
                atom_type1 = atom.property("ambertype1")

                # Set LJ/charge based on requested perturbed term.
                if perturbation_type == "discharge_soft":
                    if (atom.property("element0") == _SireMol.Element("X") or
                        atom.property("element1") == _SireMol.Element("X")):

                        # If perturbing TO dummy:
                        if atom.property("element1") == _SireMol.Element("X"):

                            atom_type1 = atom_type0

                            # In this step, only remove charges from soft-core perturbations.
                            LJ0_value = LJ1_value = LJ0.sigma().value(), LJ0.epsilon().value()

                            charge0_value = atom.property("charge0").value()
                            charge1_value = -0.0

                        # If perturbing FROM dummy:
                        else:
                            # All terms have already been perturbed in "5_grow_soft".
                            atom_type1 = atom_type0
                            LJ0_value = LJ1_value = LJ0.sigma().value(), LJ0.epsilon().value()
                            charge0_value = charge1_value = atom.property("charge0").value()

                    else:
                        # If only hard atoms in perturbation, hold parameters.
                        atom_type1 = atom_type0
                        LJ0_value = LJ1_value = LJ0.sigma().value(), LJ0.epsilon().value()
                        charge0_value = charge1_value = atom.property("charge0").value()

                elif perturbation_type == "vanish_soft":
                    if (atom.property("element0") == _SireMol.Element("X") or
                        atom.property("element1") == _SireMol.Element("X")):

                        # If perturbing TO dummy:
                        if atom.property("element1") == _SireMol.Element("X"):
                            # allow atom types to change.
                            atom_type0 = atom_type0
                            atom_type1 = atom_type1

                            # In this step, only remove LJ from soft-core perturbations.
                            LJ0_value = LJ0.sigma().value(), LJ0.epsilon().value()
                            LJ1_value = (0.0, 0.0)

                            # soft discharge was previous step, so assume 0.0.
                            charge0_value = charge1_value = -0.0

                        # If perturbing FROM dummy:
                        else:

                            # All terms have already been perturbed in "5_grow_soft".
                            atom_type1 = atom_type0
                            LJ0_value = LJ1_value = LJ0.sigma().value(), LJ0.epsilon().value()
                            charge0_value = charge1_value = atom.property("charge0").value()


                    else:
                        # If only hard atoms in perturbation, hold parameters.
                        atom_type1 = atom_type0
                        LJ0_value = LJ1_value = LJ0.sigma().value(), LJ0.epsilon().value()
                        charge0_value = charge1_value = atom.property("charge0").value()

                elif perturbation_type == "flip":
                    if (atom.property("element0") == _SireMol.Element("X") or
                        atom.property("element1") == _SireMol.Element("X")):

                        # If perturbing TO dummy:
                        if atom.property("element1") == _SireMol.Element("X"):
                            # atom types have already been changed.
                            atom_type0 = atom_type1

                            # In previous steps, soft-core transformations were discharged and vanished.
                            LJ0_value = LJ1_value = (0.0, 0.0)
                            charge0_value = charge1_value = -0.0

                        # If perturbing FROM dummy:
                        else:
                            # All terms have already been perturbed in "5_grow_soft".
                            atom_type1 = atom_type0
                            LJ0_value = LJ1_value = LJ0.sigma().value(), LJ0.epsilon().value()
                            charge0_value = charge1_value = atom.property("charge0").value()

                    else:
                        # If only hard atoms in perturbation, change all parameters.
                        atom_type1 = atom_type1
                        atom_type0 = atom_type0
                        LJ0_value = LJ0.sigma().value(), LJ0.epsilon().value()
                        LJ1_value = LJ1.sigma().value(), LJ1.epsilon().value()
                        charge0_value = atom.property("charge0").value()
                        charge1_value = atom.property("charge1").value()

                elif perturbation_type == "grow_soft":
                    if (atom.property("element0") == _SireMol.Element("X") or
                        atom.property("element1") == _SireMol.Element("X")):

                        # If perturbing TO dummy:
                        if atom.property("element1") == _SireMol.Element("X"):
                            # atom types have already been changed.
                            atom_type0 = atom_type1

                            # In previous steps, soft-core transformations were discharged and vanished.
                            LJ0_value = LJ1_value = (0.0, 0.0)
                            charge0_value = charge1_value = -0.0

                        # If perturbing FROM dummy:
                        else:
                            # if perturbing FROM dummy, i.e. element0 is dummy, perturb.
                            # allow atom types to change.
                            atom_type0 = atom_type0
                            atom_type1 = atom_type1

                            # In this step, soft-core perturbations are grown from 0.
                            LJ0_value = LJ0.sigma().value(), LJ0.epsilon().value()
                            LJ1_value = LJ1.sigma().value(), LJ1.epsilon().value()
                            charge0_value = charge1_value = atom.property("charge0").value()

                    else:
                        # If only hard atoms in perturbation, parameters are already changed.
                        atom_type0 = atom_type1
                        LJ0_value = LJ1_value = LJ1.sigma().value(), LJ1.epsilon().value()
                        charge0_value = charge1_value = atom.property("charge1").value()

                elif perturbation_type == "charge_soft":
                    if (atom.property("element0") == _SireMol.Element("X") or
                        atom.property("element1") == _SireMol.Element("X")):

                        # If perturbing TO dummy:
                        if atom.property("element1") == _SireMol.Element("X"):
                            # atom types have already been changed.
                            atom_type0 = atom_type1

                            # In previous steps, soft-core transformations were discharged and vanished.
                            LJ0_value = LJ1_value = (0.0, 0.0)
                            charge0_value = charge1_value = -0.0

                        # If perturbing FROM dummy:
                        else:
                            # if perturbing FROM dummy, i.e. element0 is dummy, perturb.
                            # atom types is already changed:
                            atom_type0 = atom_type1

                            # In this step, soft-core perturbations are charged from 0.
                            LJ0_value = LJ1_value = LJ1.sigma().value(), LJ1.epsilon().value()
                            charge0_value = atom.property("charge0").value()
                            charge1_value = atom.property("charge1").value()

                    else:
                        # If only hard atoms in perturbation, parameters are already changed.
                        atom_type0 = atom_type1
                        LJ0_value = LJ1_value = LJ1.sigma().value(), LJ1.epsilon().value()
                        charge0_value = charge1_value = atom.property("charge1").value()

                # Write atom data.
                file.write("        name           %s\n" % atom.name().value())
                file.write("        initial_type   %s\n" % atom_type0)
                file.write("        final_type     %s\n" % atom_type1)
                file.write("        initial_LJ     %.5f %.5f\n" % (LJ0_value))
                file.write("        final_LJ       %.5f %.5f\n" % (LJ1_value))
                file.write("        initial_charge %.5f\n" % charge0_value)
                file.write("        final_charge   %.5f\n" % charge1_value)

                # End atom record.
                file.write("    endatom\n")

        # 2) Bonds.

        # Extract the bonds at lambda = 0 and 1.
        bonds0 = mol.property("bond0").potentials()
        bonds1 = mol.property("bond1").potentials()

        # Dictionaries to store the BondIDs at lambda = 0 and 1.
        bonds0_idx = {}
        bonds1_idx = {}

        # Loop over all bonds at lambda = 0.
        for idx, bond in enumerate(bonds0):
            # Get the AtomIdx for the atoms in the bond.
            idx0 = info.atomIdx(bond.atom0())
            idx1 = info.atomIdx(bond.atom1())

            # Create the BondID.
            bond_id = _SireMol.BondID(idx0, idx1)

            # Add to the list of ids.
            bonds0_idx[bond_id] = idx

        # Loop over all bonds at lambda = 1.
        for idx, bond in enumerate(bonds1):
            # Get the AtomIdx for the atoms in the bond.
            idx0 = info.atomIdx(bond.atom0())
            idx1 = info.atomIdx(bond.atom1())

            # Create the AngleID.
            bond_id = _SireMol.BondID(idx0, idx1)

            # Add to the list of ids.
            if bond_id.mirror() in bonds0_idx:
                bonds1_idx[bond_id.mirror()] = idx
            else:
                bonds1_idx[bond_id] = idx

        # Now work out the BondIDs that are unique at lambda = 0 and 1
        # as well as those that are shared.
        bonds0_unique_idx = {}
        bonds1_unique_idx = {}
        bonds_shared_idx = {}

        # lambda = 0.
        for idx in bonds0_idx.keys():
            if idx not in bonds1_idx.keys():
                bonds0_unique_idx[idx] = bonds0_idx[idx]
            else:
                bonds_shared_idx[idx] = (bonds0_idx[idx], bonds1_idx[idx])

        # lambda = 1.
        for idx in bonds1_idx.keys():
            if idx not in bonds0_idx.keys():
                bonds1_unique_idx[idx] = bonds1_idx[idx]
            elif idx not in bonds_shared_idx.keys():
                bonds_shared_idx[idx] = (bonds0_idx[idx], angles1_idx[idx])

        # First create records for the bonds that are unique to lambda = 0 and 1.

        def sort_bonds(bonds, idx):
            # Get the bond potential.
            bond = bonds[idx]

            # Get the AtomIdx for the atoms in the bond.
            idx0 = info.atomIdx(bond.atom0())
            idx1 = info.atomIdx(bond.atom1())

            return (mol.atom(idx0).name().value(),
                    mol.atom(idx1).name().value())

        # lambda = 0.
        for idx in sorted(bonds0_unique_idx.values(),
                          key=lambda idx: sort_bonds(bonds0, idx)):
            # Get the bond potential.
            bond = bonds0[idx]

            # Get the AtomIdx for the atoms in the bond.
            idx0 = info.atomIdx(bond.atom0())
            idx1 = info.atomIdx(bond.atom1())

            # Cast the function as an AmberBond.
            amber_bond = _SireMM.AmberBond(bond.function(), _SireCAS.Symbol("r"))

            # Start bond record.
            file.write("    bond\n")

            # Bond data.
            file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
            file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
            file.write("        initial_force  %.5f\n" % amber_bond.k())
            file.write("        initial_equil  %.5f\n" % amber_bond.r0())
            file.write("        final_force    %.5f\n" % 0.0)
            file.write("        final_equil    %.5f\n" % amber_bond.r0())

            # End bond record.
            file.write("    endbond\n")

        # lambda = 1.
        for idx in sorted(bonds1_unique_idx.values(),
                          key=lambda idx: sort_bonds(bonds1, idx)):
            # Get the bond potential.
            bond = bonds1[idx]

            # Get the AtomIdx for the atoms in the bond.
            idx0 = info.atomIdx(bond.atom0())
            idx1 = info.atomIdx(bond.atom1())

            # Cast the function as an AmberBond.
            amber_bond = _SireMM.AmberBond(bond.function(), _SireCAS.Symbol("r"))

            # Start bond record.
            file.write("    bond\n")
            if perturbation_type in ["discharge_soft", "vanish_soft"]:
                # Bond data is unchanged.
                file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
                file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
                file.write("        initial_force  %.5f\n" % 0.0)
                file.write("        initial_equil  %.5f\n" % amber_bond.r0())
                file.write("        final_force    %.5f\n" % 0.0)
                file.write("        final_equil    %.5f\n" % amber_bond.r0())

            elif perturbation_type in ["flip", "full"]:
                # Bonds are perturbed.
                file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
                file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
                file.write("        initial_force  %.5f\n" % 0.0)
                file.write("        initial_equil  %.5f\n" % amber_bond.r0())
                file.write("        final_force    %.5f\n" % amber_bond.k())
                file.write("        final_equil    %.5f\n" % amber_bond.r0())

            elif perturbation_type in ["grow_soft", "charge_soft"]:
                # Bond data has already been changed, assume endpoints.
                file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
                file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
                file.write("        initial_force  %.5f\n" % amber_bond.k())
                file.write("        initial_equil  %.5f\n" % amber_bond.r0())
                file.write("        final_force    %.5f\n" % amber_bond.k())
                file.write("        final_equil    %.5f\n" % amber_bond.r0())

            # End bond record.
            file.write("    endbond\n")

        # Now add records for the shared bonds.
        for idx0, idx1 in sorted(bonds_shared_idx.values(),
                                 key=lambda idx_pair: sort_bonds(bonds0, idx_pair[0])):
            # Get the bond potentials.
            bond0 = bonds0[idx0]
            bond1 = bonds1[idx1]

            # Get the AtomIdx for the atoms in the bond.
            idx0 = info.atomIdx(bond0.atom0())
            idx1 = info.atomIdx(bond0.atom1())

            # Check that an atom in the bond is perturbed.
            if _has_pert_atom([idx0, idx1], pert_idxs):

                # Cast the bonds as AmberBonds.
                amber_bond0 = _SireMM.AmberBond(bond0.function(), _SireCAS.Symbol("r"))
                amber_bond1 = _SireMM.AmberBond(bond1.function(), _SireCAS.Symbol("r"))

                # Check whether a dummy atoms are present in the lambda = 0
                # and lambda = 1 states.
                initial_dummy = _has_dummy(mol, [idx0, idx1])
                final_dummy = _has_dummy(mol, [idx0, idx1], True)

                # Cannot have a bond with a dummy in both states.
                if initial_dummy and final_dummy:
                    raise _IncompatibleError("Dummy atoms are present in both the initial "
                                             "and final bond?")

                # Set the bond parameters of the dummy state to those of the non-dummy end state.
                if initial_dummy or final_dummy:
                    has_dummy = True
                    if initial_dummy:
                        amber_bond0 = amber_bond1
                    else:
                        amber_bond1 = amber_bond0
                else:
                    has_dummy = False

                # Only write record if the bond parameters change.
                if has_dummy or amber_bond0 != amber_bond1:

                    # Start bond record.
                    file.write("    bond\n")

                    if perturbation_type in ["discharge_soft", "vanish_soft"]:
                        # Bonds are not perturbed.
                        file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
                        file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
                        file.write("        initial_force  %.5f\n" % amber_bond0.k())
                        file.write("        initial_equil  %.5f\n" % amber_bond0.r0())
                        file.write("        final_force    %.5f\n" % amber_bond0.k())
                        file.write("        final_equil    %.5f\n" % amber_bond0.r0())

                    elif perturbation_type in ["flip", "full"]:
                        # Bonds are perturbed.
                        file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
                        file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
                        file.write("        initial_force  %.5f\n" % amber_bond0.k())
                        file.write("        initial_equil  %.5f\n" % amber_bond0.r0())
                        file.write("        final_force    %.5f\n" % amber_bond1.k())
                        file.write("        final_equil    %.5f\n" % amber_bond1.r0())

                    elif perturbation_type in ["grow_soft", "charge_soft"]:
                        # Bonds are already perturbed.
                        file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
                        file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
                        file.write("        initial_force  %.5f\n" % amber_bond1.k())
                        file.write("        initial_equil  %.5f\n" % amber_bond1.r0())
                        file.write("        final_force    %.5f\n" % amber_bond1.k())
                        file.write("        final_equil    %.5f\n" % amber_bond1.r0())

                    # End bond record.
                    file.write("    endbond\n")

        # 3) Angles.

        # Extract the angles at lambda = 0 and 1.
        angles0 = mol.property("angle0").potentials()
        angles1 = mol.property("angle1").potentials()

        # Dictionaries to store the AngleIDs at lambda = 0 and 1.
        angles0_idx = {}
        angles1_idx = {}

        # Loop over all angles at lambda = 0.
        for idx, angle in enumerate(angles0):
            # Get the AtomIdx for the atoms in the angle.
            idx0 = info.atomIdx(angle.atom0())
            idx1 = info.atomIdx(angle.atom1())
            idx2 = info.atomIdx(angle.atom2())

            # Create the AngleID.
            angle_id = _SireMol.AngleID(idx0, idx1, idx2)

            # Add to the list of ids.
            angles0_idx[angle_id] = idx

        # Loop over all angles at lambda = 1.
        for idx, angle in enumerate(angles1):
            # Get the AtomIdx for the atoms in the angle.
            idx0 = info.atomIdx(angle.atom0())
            idx1 = info.atomIdx(angle.atom1())
            idx2 = info.atomIdx(angle.atom2())

            # Create the AngleID.
            angle_id = _SireMol.AngleID(idx0, idx1, idx2)

            # Add to the list of ids.
            if angle_id.mirror() in angles0_idx:
                angles1_idx[angle_id.mirror()] = idx
            else:
                angles1_idx[angle_id] = idx

        # Now work out the AngleIDs that are unique at lambda = 0 and 1
        # as well as those that are shared.
        angles0_unique_idx = {}
        angles1_unique_idx = {}
        angles_shared_idx = {}

        # lambda = 0.
        for idx in angles0_idx.keys():
            if idx not in angles1_idx.keys():
                angles0_unique_idx[idx] = angles0_idx[idx]
            else:
                angles_shared_idx[idx] = (angles0_idx[idx], angles1_idx[idx])

        # lambda = 1.
        for idx in angles1_idx.keys():
            if idx not in angles0_idx.keys():
                angles1_unique_idx[idx] = angles1_idx[idx]
            elif idx not in angles_shared_idx.keys():
                angles_shared_idx[idx] = (angles0_idx[idx], angles1_idx[idx])

        # First create records for the angles that are unique to lambda = 0 and 1.

        def sort_angles(angles, idx):
            # Get the angle potential.
            angle = angles[idx]

            # Get the AtomIdx for the atoms in the angle.
            idx0 = info.atomIdx(angle.atom0())
            idx1 = info.atomIdx(angle.atom1())
            idx2 = info.atomIdx(angle.atom2())

            return (mol.atom(idx1).name().value(),
                    mol.atom(idx0).name().value(),
                    mol.atom(idx2).name().value())

        # lambda = 0.
        for idx in sorted(angles0_unique_idx.values(),
                          key=lambda idx: sort_angles(angles0, idx)):
            # Get the angle potential.
            angle = angles0[idx]

            # Get the AtomIdx for the atoms in the angle.
            idx0 = info.atomIdx(angle.atom0())
            idx1 = info.atomIdx(angle.atom1())
            idx2 = info.atomIdx(angle.atom2())

            # Cast the function as an AmberAngle.
            amber_angle = _SireMM.AmberAngle(angle.function(), _SireCAS.Symbol("theta"))

            # Start angle record.
            file.write("    angle\n")

            if perturbation_type in ["full", "flip"]:
                # Angle data.
                file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
                file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
                file.write("        atom2          %s\n" % mol.atom(idx2).name().value())
                file.write("        initial_force  %.5f\n" % amber_angle.k())
                file.write("        initial_equil  %.5f\n" % amber_angle.theta0())
                file.write("        final_force    %.5f\n" % 0.0)
                file.write("        final_equil    %.5f\n" % amber_angle.theta0())

            elif perturbation_type in ["discharge_soft", "vanish_soft"]:
                # Angle data, unperturbed.
                file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
                file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
                file.write("        atom2          %s\n" % mol.atom(idx2).name().value())
                file.write("        initial_force  %.5f\n" % amber_angle.k())
                file.write("        initial_equil  %.5f\n" % amber_angle.theta0())
                file.write("        final_force    %.5f\n" % amber_angle.k())
                file.write("        final_equil    %.5f\n" % amber_angle.theta0())

            elif perturbation_type in ["grow_soft", "charge_soft"]:
                # Angle data, already perturbed.
                file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
                file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
                file.write("        atom2          %s\n" % mol.atom(idx2).name().value())
                file.write("        initial_force  %.5f\n" % 0.0)
                file.write("        initial_equil  %.5f\n" % amber_angle.theta0())
                file.write("        final_force    %.5f\n" % 0.0)
                file.write("        final_equil    %.5f\n" % amber_angle.theta0())

            # End angle record.
            file.write("    endangle\n")

        # lambda = 1.
        for idx in sorted(angles1_unique_idx.values(),
                          key=lambda idx: sort_angles(angles1, idx)):
            # Get the angle potential.
            angle = angles1[idx]

            # Get the AtomIdx for the atoms in the angle.
            idx0 = info.atomIdx(angle.atom0())
            idx1 = info.atomIdx(angle.atom1())
            idx2 = info.atomIdx(angle.atom2())

            # Cast the function as an AmberAngle.
            amber_angle = _SireMM.AmberAngle(angle.function(), _SireCAS.Symbol("theta"))

            # Start angle record.
            file.write("    angle\n")

            if perturbation_type in ["full", "flip"]:
                # Angle data.
                file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
                file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
                file.write("        atom2          %s\n" % mol.atom(idx2).name().value())
                file.write("        initial_force  %.5f\n" % 0.0)
                file.write("        initial_equil  %.5f\n" % amber_angle.theta0())
                file.write("        final_force    %.5f\n" % amber_angle.k())
                file.write("        final_equil    %.5f\n" % amber_angle.theta0())

            elif perturbation_type in ["discharge_soft", "vanish_soft"]:
                # Angle data, unperturbed.
                file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
                file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
                file.write("        atom2          %s\n" % mol.atom(idx2).name().value())
                file.write("        initial_force  %.5f\n" % 0.0)
                file.write("        initial_equil  %.5f\n" % amber_angle.theta0())
                file.write("        final_force    %.5f\n" % 0.0)
                file.write("        final_equil    %.5f\n" % amber_angle.theta0())

            elif perturbation_type in ["grow_soft", "charge_soft"]:
                # Angle data, already perturbed.
                file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
                file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
                file.write("        atom2          %s\n" % mol.atom(idx2).name().value())
                file.write("        initial_force  %.5f\n" % amber_angle.k())
                file.write("        initial_equil  %.5f\n" % amber_angle.theta0())
                file.write("        final_force    %.5f\n" % amber_angle.k())
                file.write("        final_equil    %.5f\n" % amber_angle.theta0())

            # End angle record.
            file.write("    endangle\n")

        # Now add records for the shared angles.
        for idx0, idx1 in sorted(angles_shared_idx.values(),
                                 key=lambda idx_pair: sort_angles(angles0, idx_pair[0])):
            # Get the angle potentials.
            angle0 = angles0[idx0]
            angle1 = angles1[idx1]

            # Get the AtomIdx for the atoms in the angle.
            idx0 = info.atomIdx(angle0.atom0())
            idx1 = info.atomIdx(angle0.atom1())
            idx2 = info.atomIdx(angle0.atom2())

            # Check that an atom in the angle is perturbed.
            if _has_pert_atom([idx0, idx1, idx2], pert_idxs):

                # Cast the functions as AmberAngles.
                amber_angle0 = _SireMM.AmberAngle(angle0.function(), _SireCAS.Symbol("theta"))
                amber_angle1 = _SireMM.AmberAngle(angle1.function(), _SireCAS.Symbol("theta"))

                # Check whether a dummy atoms are present in the lambda = 0
                # and lambda = 1 states.
                initial_dummy = _has_dummy(mol, [idx0, idx1, idx2])
                final_dummy = _has_dummy(mol, [idx0, idx1, idx2], True)

                # Set the angle parameters of the dummy state to those of the non-dummy end state.
                if initial_dummy and final_dummy:
                    has_dummy = True
                    amber_angle0 = _SireMM.AmberAngle()
                    amber_angle1 = _SireMM.AmberAngle()
                elif initial_dummy or final_dummy:
                    has_dummy = True
                    if initial_dummy:
                        amber_angle0 = amber_angle1
                    else:
                        amber_angle1 = amber_angle0
                else:
                    has_dummy = False

                # Only write record if the angle parameters change.
                if has_dummy or amber_angle0 != amber_angle1:

                    # Start angle record.
                    file.write("    angle\n")

                    if perturbation_type in ["full", "flip"]:
                        # Angle data.
                        file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
                        file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
                        file.write("        atom2          %s\n" % mol.atom(idx2).name().value())
                        file.write("        initial_force  %.5f\n" % amber_angle0.k())
                        file.write("        initial_equil  %.5f\n" % amber_angle0.theta0())
                        file.write("        final_force    %.5f\n" % amber_angle1.k())
                        file.write("        final_equil    %.5f\n" % amber_angle1.theta0())

                    elif perturbation_type in ["discharge_soft", "vanish_soft"]:
                        # Angle data, unperturbed.
                        file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
                        file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
                        file.write("        atom2          %s\n" % mol.atom(idx2).name().value())
                        file.write("        initial_force  %.5f\n" % amber_angle0.k())
                        file.write("        initial_equil  %.5f\n" % amber_angle0.theta0())
                        file.write("        final_force    %.5f\n" % amber_angle0.k())
                        file.write("        final_equil    %.5f\n" % amber_angle0.theta0())

                    elif perturbation_type in ["grow_soft", "charge_soft"]:
                        # Angle data, already perturbed.
                        file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
                        file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
                        file.write("        atom2          %s\n" % mol.atom(idx2).name().value())
                        file.write("        initial_force  %.5f\n" % amber_angle1.k())
                        file.write("        initial_equil  %.5f\n" % amber_angle1.theta0())
                        file.write("        final_force    %.5f\n" % amber_angle1.k())
                        file.write("        final_equil    %.5f\n" % amber_angle1.theta0())

                    # End angle record.
                    file.write("    endangle\n")

        # 4) Dihedrals.

        # Extract the dihedrals at lambda = 0 and 1.
        dihedrals0 = mol.property("dihedral0").potentials()
        dihedrals1 = mol.property("dihedral1").potentials()

        # Dictionaries to store the DihedralIDs at lambda = 0 and 1.
        dihedrals0_idx = {}
        dihedrals1_idx = {}

        # Loop over all dihedrals at lambda = 0.
        for idx, dihedral in enumerate(dihedrals0):
            # Get the AtomIdx for the atoms in the dihedral.
            idx0 = info.atomIdx(dihedral.atom0())
            idx1 = info.atomIdx(dihedral.atom1())
            idx2 = info.atomIdx(dihedral.atom2())
            idx3 = info.atomIdx(dihedral.atom3())

            # Create the DihedralID.
            dihedral_id = _SireMol.DihedralID(idx0, idx1, idx2, idx3)

            # Add to the list of ids.
            dihedrals0_idx[dihedral_id] = idx

        # Loop over all dihedrals at lambda = 1.
        for idx, dihedral in enumerate(dihedrals1):
            # Get the AtomIdx for the atoms in the dihedral.
            idx0 = info.atomIdx(dihedral.atom0())
            idx1 = info.atomIdx(dihedral.atom1())
            idx2 = info.atomIdx(dihedral.atom2())
            idx3 = info.atomIdx(dihedral.atom3())

            # Create the DihedralID.
            dihedral_id = _SireMol.DihedralID(idx0, idx1, idx2, idx3)

            # Add to the list of ids.
            if dihedral_id.mirror() in dihedrals0_idx:
                dihedrals1_idx[dihedral_id.mirror()] = idx
            else:
                dihedrals1_idx[dihedral_id] = idx

        # Now work out the DihedralIDs that are unique at lambda = 0 and 1
        # as well as those that are shared.
        dihedrals0_unique_idx = {}
        dihedrals1_unique_idx = {}
        dihedrals_shared_idx = {}

        # lambda = 0.
        for idx in dihedrals0_idx.keys():
            if idx not in dihedrals1_idx.keys():
                dihedrals0_unique_idx[idx] = dihedrals0_idx[idx]
            else:
                dihedrals_shared_idx[idx] = (dihedrals0_idx[idx], dihedrals1_idx[idx])

        # lambda = 1.
        for idx in dihedrals1_idx.keys():
            if idx not in dihedrals0_idx.keys():
                dihedrals1_unique_idx[idx] = dihedrals1_idx[idx]
            elif idx not in dihedrals_shared_idx.keys():
                dihedrals_shared_idx[idx] = (dihedrals0_idx[idx], dihedrals1_idx[idx])

        # First create records for the dihedrals that are unique to lambda = 0 and 1.

        def sort_dihedrals(dihedrals, idx):
            # Get the dihedral potential.
            dihedral = dihedrals[idx]

            # Get the AtomIdx for the atoms in the angle.
            idx0 = info.atomIdx(dihedral.atom0())
            idx1 = info.atomIdx(dihedral.atom1())
            idx2 = info.atomIdx(dihedral.atom2())
            idx3 = info.atomIdx(dihedral.atom3())

            return (mol.atom(idx1).name().value(),
                    mol.atom(idx2).name().value(),
                    mol.atom(idx0).name().value(),
                    mol.atom(idx3).name().value())

        # lambda = 0.
        for idx in sorted(dihedrals0_unique_idx.values(),
                          key=lambda idx: sort_dihedrals(dihedrals0, idx)):
            # Get the dihedral potential.
            dihedral = dihedrals0[idx]

            # Get the AtomIdx for the atoms in the dihedral.
            idx0 = info.atomIdx(dihedral.atom0())
            idx1 = info.atomIdx(dihedral.atom1())
            idx2 = info.atomIdx(dihedral.atom2())
            idx3 = info.atomIdx(dihedral.atom3())

            # Cast the function as an AmberDihedral.
            amber_dihedral = _SireMM.AmberDihedral(dihedral.function(), _SireCAS.Symbol("phi"))

            # Start dihedral record.
            file.write("    dihedral\n")

            # Dihedral data.
            file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
            file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
            file.write("        atom2          %s\n" % mol.atom(idx2).name().value())
            file.write("        atom3          %s\n" % mol.atom(idx3).name().value())
            file.write("        initial_form  ")
            amber_dihedral_terms_sorted = sorted(
                amber_dihedral.terms(), key=lambda t: (t.k(), t.periodicity(), t.phase()))
            for term in amber_dihedral_terms_sorted:
                if perturbation_type in ["discharge_soft", "vanish_soft", "flip", "full"]:
                    file.write(" %5.4f %.1f %7.6f" % (term.k(), term.periodicity(), term.phase()))
                else:
                    file.write(" %5.4f %.1f %7.6f" % (0.0, term.periodicity(), term.phase()))
            file.write("\n")
            file.write("        final form    ")

            for term in amber_dihedral_terms_sorted:
                if perturbation_type in ["discharge_soft", "vanish_soft"]:
                    file.write(" %5.4f %.1f %7.6f" % (term.k(), term.periodicity(), term.phase()))
                else:
                    file.write(" %5.4f %.1f %7.6f" % (0.0, term.periodicity(), term.phase()))
            file.write("\n")

            # End dihedral record.
            file.write("    enddihedral\n")

        # lambda = 1.
        for idx in sorted(dihedrals1_unique_idx.values(),
                          key=lambda idx: sort_dihedrals(dihedrals1, idx)):
            # Get the dihedral potential.
            dihedral = dihedrals1[idx]

            # Get the AtomIdx for the atoms in the dihedral.
            idx0 = info.atomIdx(dihedral.atom0())
            idx1 = info.atomIdx(dihedral.atom1())
            idx2 = info.atomIdx(dihedral.atom2())
            idx3 = info.atomIdx(dihedral.atom3())

            # Cast the function as an AmberDihedral.
            amber_dihedral = _SireMM.AmberDihedral(dihedral.function(), _SireCAS.Symbol("phi"))

            # Start dihedral record.
            file.write("    dihedral\n")

            # Dihedral data.
            file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
            file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
            file.write("        atom2          %s\n" % mol.atom(idx2).name().value())
            file.write("        atom3          %s\n" % mol.atom(idx3).name().value())
            file.write("        initial_form  ")
            amber_dihedral_terms_sorted = sorted(
                amber_dihedral.terms(), key=lambda t: (t.k(), t.periodicity(), t.phase()))
            for term in amber_dihedral_terms_sorted:
                if perturbation_type in ["discharge_soft", "vanish_soft", "flip", "full"]:
                    file.write(" %5.4f %.1f %7.6f" % (0.0, term.periodicity(), term.phase()))
                else:
                    file.write(" %5.4f %.1f %7.6f" % (term.k(), term.periodicity(), term.phase()))

            file.write("\n")
            file.write("        final_form    ")
            for term in amber_dihedral_terms_sorted:
                if perturbation_type in ["discharge_soft", "vanish_soft"]:
                    file.write(" %5.4f %.1f %7.6f" % (0.0, term.periodicity(), term.phase()))
                else:
                    file.write(" %5.4f %.1f %7.6f" % (term.k(), term.periodicity(), term.phase()))
            file.write("\n")

            # End dihedral record.
            file.write("    enddihedral\n")

        # Now add records for the shared dihedrals.
        for idx0, idx1 in sorted(dihedrals_shared_idx.values(),
                                 key=lambda idx_pair: sort_dihedrals(dihedrals0, idx_pair[0])):

            # Get the dihedral potentials.
            dihedral0 = dihedrals0[idx0]
            dihedral1 = dihedrals1[idx1]

            # Get the AtomIdx for the atoms in the dihedral.
            idx0 = info.atomIdx(dihedral0.atom0())
            idx1 = info.atomIdx(dihedral0.atom1())
            idx2 = info.atomIdx(dihedral0.atom2())
            idx3 = info.atomIdx(dihedral0.atom3())

            # Check that an atom in the dihedral is perturbed.
            if _has_pert_atom([idx0, idx1, idx2, idx3], pert_idxs):

                # Cast the functions as AmberDihedrals.
                amber_dihedral0 = _SireMM.AmberDihedral(dihedral0.function(), _SireCAS.Symbol("phi"))
                amber_dihedral1 = _SireMM.AmberDihedral(dihedral1.function(), _SireCAS.Symbol("phi"))

                # Whether to zero the barrier height of the initial state dihedral.
                zero_k = False

                # Whether to force writing the dihedral to the perturbation file.
                force_write = False

                # Whether any atom in each end state is a dummy.
                has_dummy_initial = _has_dummy(mol, [idx0, idx1, idx2, idx3])
                has_dummy_final = _has_dummy(mol, [idx0, idx1, idx2, idx3], True)

                # Whether all atoms in each state are dummies.
                all_dummy_initial = all(_is_dummy(mol, [idx0, idx1, idx2, idx3]))
                all_dummy_final = all(_is_dummy(mol, [idx0, idx1, idx2, idx3], True))

                # Dummies are present in both end states, use null potentials.
                if has_dummy_initial and has_dummy_final:
                    amber_dihedral0 = _SireMM.AmberDihedral()
                    amber_dihedral1 = _SireMM.AmberDihedral()

                # Dummies in the initial state.
                elif has_dummy_initial:
                    if all_dummy_initial and not zero_dummy_dihedrals:
                        # Use the potential at lambda = 1 and write to the pert file.
                        amber_dihedral0 = amber_dihedral1
                        force_write = True
                    else:
                        zero_k = True

                # Dummies in the final state.
                elif has_dummy_final:
                    if all_dummy_final and not zero_dummy_dihedrals:
                        # Use the potential at lambda = 0 and write to the pert file.
                        amber_dihedral1 = amber_dihedral0
                        force_write = True
                    else:
                        zero_k = True

                # Only write record if the dihedral parameters change.
                if zero_k or force_write or amber_dihedral0 != amber_dihedral1:

                    # Initialise a null dihedral.
                    null_dihedral = _SireMM.AmberDihedral()

                    # Don't write the dihedral record if both potentials are null.
                    if not (amber_dihedral0 == null_dihedral and amber_dihedral1 == null_dihedral):

                        # Start dihedral record.
                        file.write("    dihedral\n")

                        # Dihedral data.
                        file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
                        file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
                        file.write("        atom2          %s\n" % mol.atom(idx2).name().value())
                        file.write("        atom3          %s\n" % mol.atom(idx3).name().value())
                        file.write("        initial_form  ")

                        if perturbation_type in ["full", "flip"]:
                            # Do the full approach.
                            for term in sorted(amber_dihedral0.terms(),
                                               key=lambda t: (t.k(), t.periodicity(), t.phase())):
                                if zero_k and has_dummy_initial:
                                    k = 0.0
                                else:
                                    k = term.k()
                                file.write(" %5.4f %.1f %7.6f" % (k, term.periodicity(), term.phase()))
                            file.write("\n")
                            file.write("        final_form    ")
                            for term in sorted(amber_dihedral1.terms(),
                                               key=lambda t: (t.k(), t.periodicity(), t.phase())):
                                if zero_k and has_dummy_final:
                                    k = 0.0
                                else:
                                    k = term.k()
                                file.write(" %5.4f %.1f %7.6f" % (k, term.periodicity(), term.phase()))
                            file.write("\n")

                        elif perturbation_type in ["discharge_soft", "vanish_soft"]:
                            # Don't perturb dihedrals, i.e. change k1 to k0.
                            for term in sorted(amber_dihedral0.terms(),
                                               key=lambda t: (t.k(), t.periodicity(), t.phase())):
                                if zero_k and has_dummy_initial:
                                    k = 0.0
                                else:
                                    k = term.k()

                                file.write(" %5.4f %.1f %7.6f" % (k, term.periodicity(), term.phase()))

                            file.write("\n")
                            file.write("        final_form    ")

                            # Looping over amber_dihedral0 instead of amber_dihedral1!
                            for term in sorted(amber_dihedral0.terms(),
                                               key=lambda t: (t.k(), t.periodicity(), t.phase())):
                                if zero_k and has_dummy_initial:
                                    k = 0.0
                                else:
                                    k = term.k()

                                file.write(" %5.4f %.1f %7.6f" % (k, term.periodicity(), term.phase()))
                            file.write("\n")

                        else:
                            # Dihedrals are already perturbed, i.e. change k0 to k1.
                            for term in sorted(amber_dihedral1.terms(),
                                               key=lambda t: (t.k(), t.periodicity(), t.phase())):
                                # Both checks are for has_dummy_final.
                                if zero_k and has_dummy_final:
                                    k = 0.0
                                else:
                                    k = term.k()
                                file.write(" %5.4f %.1f %7.6f" % (k, term.periodicity(), term.phase()))
                            file.write("\n")
                            file.write("        final_form    ")
                            for term in sorted(amber_dihedral1.terms(),
                                               key=lambda t: (t.k(), t.periodicity(), t.phase())):
                                if zero_k and has_dummy_final:
                                    k = 0.0
                                else:
                                    k = term.k()
                                file.write(" %5.4f %.1f %7.6f" % (k, term.periodicity(), term.phase()))
                            file.write("\n")
                        # End dihedral record.
                        file.write("    enddihedral\n")

        # 5) Impropers.

        # Extract the impropers at lambda = 0 and 1.
        impropers0 = mol.property("improper0").potentials()
        impropers1 = mol.property("improper1").potentials()

        # Dictionaries to store the ImproperIDs at lambda = 0 and 1.
        impropers0_idx = {}
        impropers1_idx = {}

        # Loop over all impropers at lambda = 0.
        for idx, improper in enumerate(impropers0):
            # Get the AtomIdx for the atoms in the improper.
            idx0 = info.atomIdx(improper.atom0())
            idx1 = info.atomIdx(improper.atom1())
            idx2 = info.atomIdx(improper.atom2())
            idx3 = info.atomIdx(improper.atom3())

            # Create the ImproperID.
            improper_id = _SireMol.ImproperID(idx0, idx1, idx2, idx3)

            # Add to the list of ids.
            impropers0_idx[improper_id] = idx

        # Loop over all impropers at lambda = 1.
        for idx, improper in enumerate(impropers1):
            # Get the AtomIdx for the atoms in the improper.
            idx0 = info.atomIdx(improper.atom0())
            idx1 = info.atomIdx(improper.atom1())
            idx2 = info.atomIdx(improper.atom2())
            idx3 = info.atomIdx(improper.atom3())

            # Create the ImproperID.
            improper_id = _SireMol.ImproperID(idx0, idx1, idx2, idx3)

            # Add to the list of ids.
            # You cannot mirror an improper!
            impropers1_idx[improper_id] = idx

        # Now work out the ImproperIDs that are unique at lambda = 0 and 1
        # as well as those that are shared. Note that the ordering of
        # impropers is inconsistent between molecular topology formats so
        # we test all permutations of atom ordering to find matches. This
        # is achieved using the ImproperID.equivalent() method.
        impropers0_unique_idx = {}
        impropers1_unique_idx = {}
        impropers_shared_idx = {}

        # lambda = 0.
        for idx0 in impropers0_idx.keys():
            is_shared = False
            for idx1 in impropers1_idx.keys():
                if idx0.equivalent(idx1):
                    impropers_shared_idx[idx0] = (impropers0_idx[idx0], impropers1_idx[idx1])
                    is_shared = True
                    break
            if not is_shared:
                impropers0_unique_idx[idx0] = impropers0_idx[idx0]

        # lambda = 1.
        for idx1 in impropers1_idx.keys():
            is_shared = False
            for idx0 in impropers0_idx.keys():
                if idx1.equivalent(idx0):
                    # Don't store duplicates.
                    if not idx0 in impropers_shared_idx.keys():
                        impropers_shared_idx[idx1] = (impropers0_idx[idx0], impropers1_idx[idx1])
                    is_shared = True
                    break
            if not is_shared:
                impropers1_unique_idx[idx1] = impropers1_idx[idx1]

        # First create records for the impropers that are unique to lambda = 0 and 1.

        # lambda = 0.
        for idx in impropers0_unique_idx.values():
            # Get the improper potential.
            improper = impropers0[idx]

            # Get the AtomIdx for the atoms in the improper.
            idx0 = info.atomIdx(improper.atom0())
            idx1 = info.atomIdx(improper.atom1())
            idx2 = info.atomIdx(improper.atom2())
            idx3 = info.atomIdx(improper.atom3())

            # Cast the function as an AmberDihedral.
            amber_dihedral = _SireMM.AmberDihedral(improper.function(), _SireCAS.Symbol("phi"))

            # Start improper record.
            file.write("    improper\n")

            # Dihedral data.
            file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
            file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
            file.write("        atom2          %s\n" % mol.atom(idx2).name().value())
            file.write("        atom3          %s\n" % mol.atom(idx3).name().value())
            file.write("        initial_form  ")
            amber_dihedral_terms_sorted = sorted(
                amber_dihedral.terms(), key=lambda t: (t.k(), t.periodicity(), t.phase()))
            for term in amber_dihedral_terms_sorted:
                if perturbation_type in [
                    "discharge_soft",
                    "vanish_soft",
                    "flip",
                    "full"]:
                    file.write(" %5.4f %.1f %7.6f" % (term.k(), term.periodicity(), term.phase()))
                else:
                    file.write(" %5.4f %.1f %7.6f" % (0.0, term.periodicity(), term.phase()))

            file.write("\n")
            file.write("        final form    ")
            for term in amber_dihedral_terms_sorted:
                if perturbation_type in [
                    "discharge_soft",
                    "vanish_soft"]:
                    file.write(" %5.4f %.1f %7.6f" % (term.k(), term.periodicity(), term.phase()))

                else:
                    file.write(" %5.4f %.1f %7.6f" % (0.0, term.periodicity(), term.phase()))
            file.write("\n")

            # End improper record.
            file.write("    endimproper\n")

        # lambda = 1.
        for idx in impropers1_unique_idx.values():
            # Get the improper potential.
            improper = impropers1[idx]

            # Get the AtomIdx for the atoms in the dihedral.
            idx0 = info.atomIdx(improper.atom0())
            idx1 = info.atomIdx(improper.atom1())
            idx2 = info.atomIdx(improper.atom2())
            idx3 = info.atomIdx(improper.atom3())

            # Cast the function as an AmberDihedral.
            amber_dihedral = _SireMM.AmberDihedral(improper.function(), _SireCAS.Symbol("phi"))

            # Start improper record.
            file.write("    improper\n")

            # Improper data.
            file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
            file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
            file.write("        atom2          %s\n" % mol.atom(idx2).name().value())
            file.write("        atom3          %s\n" % mol.atom(idx3).name().value())
            file.write("        initial_form  ")
            for term in sorted(amber_dihedral0.terms(),
                               key=lambda t: (t.k(), t.periodicity(), t.phase())):
                if perturbation_type in ["discharge_soft", "vanish_soft", "flip", "full"]:
                    file.write(" %5.4f %.1f %7.6f" % (0.0, term.periodicity(), term.phase()))
                else:
                    file.write(" %5.4f %.1f %7.6f" % (term.k(), term.periodicity(), term.phase()))
            file.write("\n")
            file.write("        final_form    ")
            for term in sorted(amber_dihedral1.terms(),
                               key=lambda t: (t.k(), t.periodicity(), t.phase())):
                if perturbation_type in ["discharge_soft", "vanish_soft"]:
                    file.write(" %5.4f %.1f %7.6f" % (0.0, term.periodicity(), term.phase()))
                else:
                    file.write(" %5.4f %.1f %7.6f" % (term.k(), term.periodicity(), term.phase()))
            file.write("\n")

            # End improper record.
            file.write("    endimproper\n")

        # Now add records for the shared impropers.
        for idx0, idx1 in impropers_shared_idx.values():
            # Get the improper potentials.
            improper0 = impropers0[idx0]
            improper1 = impropers1[idx1]

            # Get the AtomIdx for the atoms in the improper.
            idx0 = info.atomIdx(improper0.atom0())
            idx1 = info.atomIdx(improper0.atom1())
            idx2 = info.atomIdx(improper0.atom2())
            idx3 = info.atomIdx(improper0.atom3())

            # Check that an atom in the improper is perturbed.
            if _has_pert_atom([idx0, idx1, idx2, idx3], pert_idxs):

                # Cast the functions as AmberDihedrals.
                amber_dihedral0 = _SireMM.AmberDihedral(improper0.function(), _SireCAS.Symbol("phi"))
                amber_dihedral1 = _SireMM.AmberDihedral(improper1.function(), _SireCAS.Symbol("phi"))

                # Whether to zero the barrier height of the initial/final improper.
                zero_k = False

                # Whether to force writing the improper to the perturbation file.
                force_write = False

                # Whether any atom in each end state is a dummy.
                has_dummy_initial = _has_dummy(mol, [idx0, idx1, idx2, idx3])
                has_dummy_final = _has_dummy(mol, [idx0, idx1, idx2, idx3], True)

                # Whether all atoms in each state are dummies.
                all_dummy_initial = all(_is_dummy(mol, [idx0, idx1, idx2, idx3]))
                all_dummy_final = all(_is_dummy(mol, [idx0, idx1, idx2, idx3], True))

                # Dummies are present in both end states, use null potentials.
                if has_dummy_initial and has_dummy_final:
                    amber_dihedral0 = _SireMM.AmberDihedral()
                    amber_dihedral1 = _SireMM.AmberDihedral()

                # Dummies in the initial state.
                elif has_dummy_initial:
                    if all_dummy_initial and not zero_dummy_impropers:
                        # Use the potential at lambda = 1 and write to the pert file.
                        amber_dihedral0 = amber_dihedral1
                        force_write = True
                    else:
                        zero_k = True

                # Dummies in the final state.
                elif has_dummy_final:
                    if all_dummy_final and not zero_dummy_impropers:
                        # Use the potential at lambda = 0 and write to the pert file.
                        amber_dihedral1 = amber_dihedral0
                        force_write = True
                    else:
                        zero_k = True

                # Only write record if the improper parameters change.
                if zero_k or force_write or amber_dihedral0 != amber_dihedral1:

                    # Initialise a null dihedral.
                    null_dihedral = _SireMM.AmberDihedral()

                    # Don't write the improper record if both potentials are null.
                    if not (amber_dihedral0 == null_dihedral and amber_dihedral1 == null_dihedral):

                        # Start improper record.
                        file.write("    improper\n")

                        # Improper data.
                        file.write("        atom0          %s\n" % mol.atom(idx0).name().value())
                        file.write("        atom1          %s\n" % mol.atom(idx1).name().value())
                        file.write("        atom2          %s\n" % mol.atom(idx2).name().value())
                        file.write("        atom3          %s\n" % mol.atom(idx3).name().value())
                        file.write("        initial_form  ")

                        if perturbation_type in ["full", "flip"]:
                            # Do the full approach.
                            for term in sorted(amber_dihedral0.terms(),
                                               key=lambda t: (t.k(), t.periodicity(), t.phase())):
                                if zero_k and has_dummy_initial:
                                    k = 0.0
                                else:
                                    k = term.k()
                                file.write(" %5.4f %.1f %7.6f" % (k, term.periodicity(), term.phase()))
                            file.write("\n")
                            file.write("        final_form    ")
                            for term in sorted(amber_dihedral1.terms(),
                                               key=lambda t: (t.k(), t.periodicity(), t.phase())):
                                if zero_k and has_dummy_final:
                                    k = 0.0
                                else:
                                    k = term.k()
                                file.write(" %5.4f %.1f %7.6f" % (k, term.periodicity(), term.phase()))
                            file.write("\n")

                        elif perturbation_type in ["discharge_soft", "vanish_soft"]:
                            # Don't perturb dihedrals, i.e. change k1 to k0.
                            for term in sorted(amber_dihedral0.terms(),
                                               key=lambda t: (t.k(), t.periodicity(), t.phase())):
                                if zero_k and has_dummy_initial:
                                    k = 0.0
                                else:
                                    k = term.k()

                                file.write(" %5.4f %.1f %7.6f" % (k, term.periodicity(), term.phase()))

                            file.write("\n")
                            file.write("        final_form    ")
                            # looping over amber_dihedral0 instead of amber_dihedral1!
                            for term in sorted(amber_dihedral0.terms(),
                                               key=lambda t: (t.k(), t.periodicity(), t.phase())):
                                if zero_k and has_dummy_initial:
                                    k = 0.0
                                else:
                                    k = term.k()

                                file.write(" %5.4f %.1f %7.6f" % (k, term.periodicity(), term.phase()))
                            file.write("\n")

                        else:
                            # Dihedrals are already perturbed, i.e. change k0 to k1.
                            for term in sorted(amber_dihedral1.terms(),
                                               key=lambda t: (t.k(), t.periodicity(), t.phase())):
                                # Both checks are for has_dummy_final.
                                if zero_k and has_dummy_final:
                                    k = 0.0
                                else:
                                    k = term.k()
                                file.write(" %5.4f %.1f %7.6f" % (k, term.periodicity(), term.phase()))
                            file.write("\n")
                            file.write("        final_form    ")
                            for term in sorted(amber_dihedral1.terms(),
                                               key=lambda t: (t.k(), t.periodicity(), t.phase())):
                                if zero_k and has_dummy_final:
                                    k = 0.0
                                else:
                                    k = term.k()
                                file.write(" %5.4f %.1f %7.6f" % (k, term.periodicity(), term.phase()))
                            file.write("\n")

                        # End improper record.
                        file.write("    endimproper\n")

        # End molecule record.
        file.write("endmolecule\n")

    # Finally, convert the molecule to the lambda = 0 state.

    # Make the molecule editable.
    mol = mol.edit()

    # Remove the perturbable molecule flag.
    mol = mol.removeProperty("is_perturbable").molecule()

    # Special handling for the mass and element properties. Perturbed atoms
    # take the mass and atomic number from the maximum of both states,
    # not the lambda = 0 state.
    if mol.hasProperty("mass0") and mol.hasProperty("element0"):
        # See if the mass or element properties exists in the user map.
        new_mass_prop = property_map.get("mass", "mass")
        new_element_prop = property_map.get("element", "element")

        for idx in range(0, mol.nAtoms()):
            # Convert to an AtomIdx.
            idx = _SireMol.AtomIdx(idx)

            # Extract the elements of the end states.
            element0 = mol.atom(idx).property("element0")
            element1 = mol.atom(idx).property("element1")

            # The end states are different elements.
            if element0 != element1:
                # Extract the mass of the end states.
                mass0 = mol.atom(idx).property("mass0")
                mass1 = mol.atom(idx).property("mass1")

                # Choose the heaviest mass.
                if mass0.value() > mass1.value():
                    mass = mass0
                else:
                    mass = mass1

                # Choose the element with the most protons.
                if element0.nProtons() > element1.nProtons():
                    element = element0
                else:
                    element = element1

                # Set the updated properties.
                mol = mol.atom(idx).setProperty(new_mass_prop, mass).molecule()
                mol = mol.atom(idx).setProperty(new_element_prop, element).molecule()

            else:
                # Use the properties at lambda = 0.
                mass = mol.atom(idx).property("mass0")
                mol = mol.atom(idx).setProperty(new_mass_prop, mass).molecule()
                mol = mol.atom(idx).setProperty(new_element_prop, element0).molecule()

        # Delete redundant properties.
        mol = mol.removeProperty("mass0").molecule()
        mol = mol.removeProperty("mass1").molecule()
        mol = mol.removeProperty("element0").molecule()
        mol = mol.removeProperty("element1").molecule()

    # Rename all properties in the molecule: "prop0" --> "prop".
    # Delete all properties named "prop0" and "prop1".
    for prop in mol.propertyKeys():
        if prop[-1] == "0" and prop != "mass0" and prop != "element0":
            # See if this property exists in the user map.
            new_prop = property_map.get(prop[:-1], prop[:-1])

            # Copy the property using the updated name.
            mol = mol.setProperty(new_prop, mol.property(prop)).molecule()

            # Delete redundant properties.
            mol = mol.removeProperty(prop).molecule()
            mol = mol.removeProperty(prop[:-1] + "1").molecule()

    # Return the updated molecule.
    return mol.commit()

def _has_pert_atom(idxs, pert_idxs):
    """Internal function to check whether a potential contains perturbed atoms.

       Parameters
       ----------

       idxs : [AtomIdx]
           A list of atom indices involved in the potential.

       pert_idxs : [AtomIdx]
           A list of atom indices that are perturbed.

       Returns
       -------

       has_pert_atom : bool
           Whether the potential includes a perturbed atom.
    """

    for idx in idxs:
        if idx in pert_idxs:
            return True

    return False

def _has_dummy(mol, idxs, is_lambda1=False):
    """Internal function to check whether any atom is a dummy.

       Parameters
       ----------

       mol : Sire.Mol.Molecule
           The molecule.

       idxs : [AtomIdx]
           A list of atom indices.

       is_lambda1 : bool
           Whether to check the lambda = 1 state.

       Returns
       -------

       has_dummy : bool
           Whether a dummy atom is present.
    """

    # Set the element property associated with the end state.
    if is_lambda1:
        prop = "element1"
    else:
        prop = "element0"

    dummy = _SireMol.Element(0)

    # Check whether an of the atoms is a dummy.
    for idx in idxs:
        if mol.atom(idx).property(prop) == dummy:
            return True

    return False

def _is_dummy(mol, idxs, is_lambda1=False):
    """Internal function to return whether each atom is a dummy.

       Parameters
       ----------

       mol : Sire.Mol.Molecule
           The molecule.

       idxs : [AtomIdx]
           A list of atom indices.

       is_lambda1 : bool
           Whether to check the lambda = 1 state.

       Returns
       -------

       is_dummy : [bool]
           Whether each atom is a dummy.
    """

    # Set the element property associated with the end state.
    if is_lambda1:
        prop = "element1"
    else:
        prop = "element0"

    # Store a dummy element.
    dummy = _SireMol.Element(0)

    # Initialise a list to store the state of each atom.
    is_dummy = []

    # Check whether each of the atoms is a dummy.
    for idx in idxs:
        is_dummy.append(mol.atom(idx).property(prop) == dummy)

    return is_dummy

def _random_suffix(basename, size=4, chars=_string.ascii_uppercase + _string.digits):
    """Internal helper function to generate a random atom name suffix to avoid
       naming clashes.

       Adapted from:
       https://stackoverflow.com/questions/2257441/random-string-generation-with-upper-case-letters-and-digits-in-python

       Parameters
       ----------

       basename : str
           The base string to which a suffix will be appended.

       size : int
           The maximum width of the string, i.e. len(basename + suffix).

       chars : str
           The set of characters to include in the suffix.

       Returns
       -------

       suffix : str
           The randomly generated suffix.
    """
    basename_size = len(basename)
    if basename_size >= size:
        raise ValueError("Cannot generate suffix for basename '%s'. " % basename
                       + "AMBER atom names can only be 4 characters wide.")
    return "".join(_random.choice(chars) for _ in range(size-basename_size))
