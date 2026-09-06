# eve/ — EVE-NG REST API client and topology builder

Builds the EVE-NG lab from `topology/*.yml`: nodes, networks (including the
`pnet1` OOB cloud), links, and startup configs. Also drives image import
(scp + `unl_wrapper -a fixpermissions`). The GUI is never the source of truth.
