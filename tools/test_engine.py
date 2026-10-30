import matlab.engine

print("Starting MATLAB engine... this may take a few seconds.")
eng = matlab.engine.start_matlab()
print("Engine started!")

# Set mode_in the same way you've been doing manually
eng.eval("mode_in = timeseries(1, 0);", nargout=0)
print("mode_in set to Mode 1.")

# Run the simulation
print("Running simulation...")
eng.eval("sim('solar_hydrogen_microgrid')", nargout=0)
print("Simulation finished successfully!")

eng.quit()
print("Engine closed.")