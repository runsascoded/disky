import { useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { AppBar, Toolbar, Typography, Button, Box, Tooltip, Popover, List, ListItem, ListItemText, Chip, Alert, ToggleButton, ToggleButtonGroup } from '@mui/material'
import { FaCloud, FaDatabase, FaFolder, FaHistory, FaCog, FaTrash } from 'react-icons/fa'
import { useQuery } from '@tanstack/react-query'
import { fetchAvailableBackends } from '../api'
import { useCapabilities } from '../hooks/useCapabilities'
import { useDeleteMethod } from '../hooks/useDeleteMethod'
import { useUnits } from '../utils/units'
import { LibrarySwitcher } from './LibrarySwitcher'
import { WhoamiChip } from '../auth'

export function Header() {
  const location = useLocation()
  const path = location.pathname
  const isScansPage = path === '/'
  const isLocalPage = path.startsWith('/file')
  const isS3Page = path.startsWith('/s3')
  const isRecentPage = path === '/recent'
  const isStagedPage = path === '/staged'
  const [units, setUnits] = useUnits()

  // Backend popover state
  const [anchorEl, setAnchorEl] = useState<HTMLButtonElement | null>(null)
  const handleBackendClick = (event: React.MouseEvent<HTMLButtonElement>) => {
    setAnchorEl(event.currentTarget)
  }
  const handleBackendClose = () => {
    setAnchorEl(null)
  }
  const backendOpen = Boolean(anchorEl)

  const caps = useCapabilities()
  const { method: deleteMethod, offered: methodOffered, setMethod: setDeleteMethod } = useDeleteMethod()
  // Fetch backend info (not on a static deployment — nothing to switch)
  const { data: backendData } = useQuery({
    queryKey: ['available-backends'],
    queryFn: fetchAvailableBackends,
    staleTime: Infinity, // Backend doesn't change during session
    enabled: caps?.backend === true,
  })

  return (
    <AppBar position="static" color="transparent" elevation={0} sx={{ borderBottom: 1, borderColor: 'divider' }}>
      <Toolbar variant="dense" sx={{ flexWrap: 'wrap', rowGap: 0.5 }}>
        <Typography variant="h6" component="div" sx={{ flexGrow: 0, mr: 4 }}>
          disk-tree
        </Typography>
        <Box sx={{ flexGrow: 1, display: 'flex', gap: 1 }}>
          <Button
            component={Link}
            to="/"
            startIcon={<FaDatabase />}
            variant={isScansPage ? 'contained' : 'text'}
            size="small"
          >
            Scans
          </Button>
          {/* Local + S3 lead to a live scanner / bucket lister the static
              deployment doesn't run; hide them when the capability is off
              (`undefined` while caps load → shown, matching the Flask default). */}
          {caps?.filesystem !== false && (
            <Button
              component={Link}
              to="/file/"
              startIcon={<FaFolder />}
              variant={isLocalPage ? 'contained' : 'text'}
              size="small"
            >
              Local
            </Button>
          )}
          {caps?.s3 !== false && (
            <Button
              component={Link}
              to="/s3/"
              startIcon={<FaCloud />}
              variant={isS3Page ? 'contained' : 'text'}
              size="small"
            >
              S3
            </Button>
          )}
          <Button
            component={Link}
            to="/recent"
            startIcon={<FaHistory />}
            variant={isRecentPage ? 'contained' : 'text'}
            size="small"
          >
            Recent
          </Button>
          {/* Staged-delete queue: shown where staging is a possible method (an
              always-sync deployment has no queue to show). */}
          {caps?.stageDelete && caps?.deleteApproval !== 'sync' && (
            <Button
              component={Link}
              to="/staged"
              startIcon={<FaTrash />}
              variant={isStagedPage ? 'contained' : 'text'}
              size="small"
            >
              Staged
            </Button>
          )}
        </Box>

        {caps?.library && <LibrarySwitcher />}

        {/* Delete method (spec `staged-delete.md` CP6): shown only where the
            deployment leaves the choice to the user (`deleteApproval:
            user-choice`); a locked deployment hides it entirely. */}
        {methodOffered && (
          <Tooltip title="Delete method — Sync: delete immediately · Staged: queue for an admin to dispatch">
            <ToggleButtonGroup
              size="small"
              exclusive
              value={deleteMethod}
              onChange={(_e, v) => { if (v) setDeleteMethod(v) }}
              sx={{ mr: 1, '& .MuiToggleButton-root': { py: 0, px: 1, fontSize: '0.7rem', textTransform: 'none' } }}
            >
              <ToggleButton value="sync">Sync</ToggleButton>
              <ToggleButton value="staged">Staged</ToggleButton>
            </ToggleButtonGroup>
          </Tooltip>
        )}

        <WhoamiChip />

        {/* Size units: SI (G = 10⁹) vs IEC (Gi = 2³⁰). Single tooltip on the
            group (wrapping each button breaks ToggleButtonGroup's value/onChange
            injection), with static text describing *both* — keying it to the
            active unit read backwards when hovering the inactive button. */}
        <Tooltip title="Size units — G: SI, powers of 1000 (Finder, df -H) · Gi: IEC, powers of 1024 (du, ls -h)">
          <ToggleButtonGroup
            size="small"
            exclusive
            value={units}
            onChange={(_e, v) => { if (v) setUnits(v) }}
            sx={{ mr: 1, '& .MuiToggleButton-root': { py: 0, px: 1, fontSize: '0.7rem', textTransform: 'none' } }}
          >
            <ToggleButton value="si">G</ToggleButton>
            <ToggleButton value="iec">Gi</ToggleButton>
          </ToggleButtonGroup>
        </Tooltip>

        {/* Backend indicator */}
        {backendData && (
          <>
            <Tooltip title="Storage backend">
              <Button
                size="small"
                onClick={handleBackendClick}
                startIcon={<FaCog />}
                sx={{ textTransform: 'none', opacity: 0.8 }}
              >
                {backendData.current}
              </Button>
            </Tooltip>
            <Popover
              open={backendOpen}
              anchorEl={anchorEl}
              onClose={handleBackendClose}
              anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
              transformOrigin={{ vertical: 'top', horizontal: 'right' }}
            >
              <Box sx={{ p: 2, minWidth: 300 }}>
                <Typography variant="subtitle2" sx={{ mb: 1 }}>Storage Backends</Typography>
                <List dense disablePadding>
                  {backendData.backends.map(b => (
                    <ListItem key={b.name} disablePadding sx={{ py: 0.5 }}>
                      <ListItemText
                        primary={
                          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                            <code style={{ fontWeight: b.current ? 600 : 400 }}>{b.name}</code>
                            {b.current && <Chip label="active" size="small" color="primary" sx={{ height: 18 }} />}
                            {b.supports_updates && <Chip label="updates" size="small" variant="outlined" sx={{ height: 18 }} />}
                          </Box>
                        }
                        secondary={b.description}
                        secondaryTypographyProps={{ variant: 'caption' }}
                      />
                    </ListItem>
                  ))}
                </List>
                <Alert severity="info" sx={{ mt: 2, py: 0 }}>
                  <Typography variant="caption">
                    To switch: <code>DISK_TREE_BACKEND=duckdb disk-tree-server</code>
                  </Typography>
                </Alert>
              </Box>
            </Popover>
          </>
        )}
      </Toolbar>
    </AppBar>
  )
}
