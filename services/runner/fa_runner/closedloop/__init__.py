"""The live closed loop: a spray job that corrects itself while it is flown.

prediction -- what the plan expects, frozen before take-off, and deviation scoring;
replan     -- the remaining job after each sortie;
run        -- the sortie loop (simulated sprayer camera, optional spot valve).
"""
